"""SQLite persistence for the OTT tournament platform."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import sqlite3
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import chain, groupby
from pathlib import Path
from typing import Any, Iterable, Iterator

from core.languages import build_key, language_profile
from judge.database import _hash_password, _verify_password
from ott.events import HUB

SESSION_TTL = 12 * 60 * 60
ENGINE_VERSION = "ott-1.0"
SCHEMA_VERSION = 4
LEASE_SECONDS = 30
MAX_ATTEMPTS = 2
LOG = logging.getLogger("ott.store")
_DUMMY_PASSWORD_HASH = _hash_password("dummy-password-value")


@dataclass(frozen=True)
class Lease:
    owner: str
    epoch: int


class LeaseLost(RuntimeError):
    """The caller no longer owns an execution lease."""

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'CONTESTANT',
    created_at INTEGER NOT NULL
);
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX sessions_expiry_idx ON sessions(expires_at);
CREATE TABLE tournaments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    game_id TEXT NOT NULL,
    game_version TEXT NOT NULL,
    clock_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    coding_seconds INTEGER NOT NULL DEFAULT 3600,
    coding_started_at INTEGER,
    coding_deadline INTEGER
);
CREATE INDEX tournaments_status_idx ON tournaments(status, created_at);
CREATE TABLE bot_submissions (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES tournaments(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    language TEXT NOT NULL,
    source TEXT,
    source_sha256 TEXT NOT NULL,
    source_size INTEGER NOT NULL,
    compile_status TEXT NOT NULL,
    compile_message TEXT,
    source_purged_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(id, tournament_id, user_id)
);
CREATE INDEX bot_submissions_entry_idx ON bot_submissions(tournament_id, user_id, created_at DESC);
CREATE TABLE entries (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES tournaments(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    bot_submission_id TEXT,
    status TEXT NOT NULL,
    registered_at INTEGER NOT NULL,
    draft_source TEXT,
    draft_updated_at INTEGER,
    submitted_at INTEGER,
    submit_note TEXT,
    compile_count INTEGER NOT NULL DEFAULT 0,
    last_ok_sha TEXT,
    UNIQUE(tournament_id, user_id),
    UNIQUE(id, tournament_id),
    FOREIGN KEY (bot_submission_id, tournament_id, user_id)
        REFERENCES bot_submissions(id, tournament_id, user_id)
);
CREATE TABLE matches (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES tournaments(id),
    round_no INTEGER NOT NULL,
    leg_no INTEGER NOT NULL,
    pairing_no INTEGER NOT NULL,
    white_entry_id TEXT NOT NULL,
    black_entry_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    result_reason TEXT,
    phase TEXT NOT NULL DEFAULT 'PLACEMENT',
    side_to_act TEXT,
    state_json TEXT,
    white_clock_ms INTEGER NOT NULL,
    black_clock_ms INTEGER NOT NULL,
    clock_checkpoint_at INTEGER,
    move_count INTEGER NOT NULL DEFAULT 0,
    started_at INTEGER,
    finished_at INTEGER,
    updated_at INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(tournament_id, leg_no, round_no, pairing_no),
    CHECK(white_entry_id <> black_entry_id),
    FOREIGN KEY (white_entry_id, tournament_id) REFERENCES entries(id, tournament_id),
    FOREIGN KEY (black_entry_id, tournament_id) REFERENCES entries(id, tournament_id)
);
CREATE INDEX matches_queue_idx ON matches(status, tournament_id, leg_no, round_no);
CREATE TABLE match_events (
    match_id TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    sequence_no INTEGER NOT NULL,
    phase TEXT NOT NULL,
    side TEXT NOT NULL,
    action_json TEXT NOT NULL,
    state_after_json TEXT NOT NULL,
    white_clock_ms INTEGER NOT NULL,
    black_clock_ms INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (match_id, sequence_no)
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    action TEXT NOT NULL,
    target TEXT,
    created_at INTEGER NOT NULL
);
"""


def now() -> int:
    return int(time.time())


class StoreError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connections = threading.local()
        self._compile_flags: dict[str, threading.Event] = {}
        self._compile_flags_lock = threading.Lock()
        self._migrate()

    def enable_thread_connection_reuse(self) -> None:
        self._connections.enabled = True

    def close_thread_connection(self) -> None:
        connection = getattr(self._connections, "connection", None)
        try:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                finally:
                    connection.close()
        finally:
            if hasattr(self._connections, "connection"):
                del self._connections.connection
            self._connections.enabled = False

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        reuse = getattr(self._connections, "enabled", False)
        con = getattr(self._connections, "connection", None) if reuse else None
        if con is None:
            con = sqlite3.connect(self.path, timeout=15)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA busy_timeout = 15000")
            if reuse:
                self._connections.connection = con
        try:
            with con:
                yield con
        finally:
            if not reuse:
                con.close()

    def _migrate(self) -> None:
        with self.tx() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("BEGIN IMMEDIATE")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        con.execute(statement)
                con.execute("PRAGMA user_version = 1")
                version = 1
            elif version > SCHEMA_VERSION:
                raise RuntimeError("Database is newer than this server.")
            if version == 1:
                for definition in (
                    "lease_owner TEXT", "lease_epoch INTEGER NOT NULL DEFAULT 0",
                    "lease_expires_at INTEGER", "attempt_count INTEGER NOT NULL DEFAULT 0",
                    "available_at INTEGER NOT NULL DEFAULT 0", "last_event_seq INTEGER NOT NULL DEFAULT 0",
                    "execution_started INTEGER NOT NULL DEFAULT 0", "execution_snapshot TEXT",
                    "result_hash TEXT", "timing_uncertainty_ms INTEGER",
                ):
                    con.execute(f"ALTER TABLE matches ADD COLUMN {definition}")
                con.execute("CREATE TABLE match_attempts (match_id TEXT NOT NULL REFERENCES matches(id), epoch INTEGER NOT NULL, owner TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER, reason TEXT, PRIMARY KEY(match_id,epoch))")
                con.execute("CREATE TABLE entry_leases (entry_id TEXT PRIMARY KEY REFERENCES entries(id), match_id TEXT NOT NULL REFERENCES matches(id), epoch INTEGER NOT NULL)")
                con.execute("CREATE TABLE resource_leases (token TEXT PRIMARY KEY, owner TEXT NOT NULL, resource TEXT NOT NULL, expires_at INTEGER NOT NULL)")
                con.execute("UPDATE matches SET last_event_seq=COALESCE((SELECT MAX(sequence_no) FROM match_events WHERE match_id=matches.id),0), execution_started=CASE WHEN state_json IS NOT NULL OR status='RUNNING' THEN 1 ELSE 0 END")
                con.execute("PRAGMA user_version = 2")
                version = 2
            con.execute("CREATE TABLE IF NOT EXISTS submission_receipts (id TEXT PRIMARY KEY, bot_id TEXT NOT NULL UNIQUE, entry_id TEXT NOT NULL REFERENCES entries(id), tournament_id TEXT NOT NULL REFERENCES tournaments(id), user_id INTEGER NOT NULL REFERENCES users(id), source TEXT NOT NULL, source_hash TEXT NOT NULL, received_at REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('QUEUED','READY','FAILED','REVIEW')), infra_attempts INTEGER NOT NULL DEFAULT 0, reason_code TEXT, created_at INTEGER NOT NULL)")
            for table, column in (("entries", "draft_language"), ("submission_receipts", "language")):
                if column not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT 'cpp17'")
            if version == 2:
                con.execute("PRAGMA user_version = 3")
                version = 3
            if version == 3:
                if "state_after_zlib" not in {row[1] for row in con.execute("PRAGMA table_info(match_events)")}:
                    con.execute("ALTER TABLE match_events ADD COLUMN state_after_zlib BLOB")
                con.execute("PRAGMA user_version = 4")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS submission_receipts_pending_entry ON submission_receipts(entry_id) WHERE status='QUEUED'")
            con.execute("CREATE INDEX IF NOT EXISTS submission_receipts_queue ON submission_receipts(status,received_at,id)")
            con.execute("CREATE INDEX IF NOT EXISTS submission_receipts_entry_status_idx ON submission_receipts(entry_id,status,received_at)")
            con.execute("CREATE INDEX IF NOT EXISTS submission_receipts_pending_entry_order_idx ON submission_receipts(entry_id,created_at,id) WHERE status IN ('QUEUED','REVIEW')")
            if "compile_charged" not in {row[1] for row in con.execute("PRAGMA table_info(submission_receipts)")}:
                con.execute("ALTER TABLE submission_receipts ADD COLUMN compile_charged INTEGER NOT NULL DEFAULT 0")
            if "free_compile" not in {row[1] for row in con.execute("PRAGMA table_info(submission_receipts)")}:
                con.execute("ALTER TABLE submission_receipts ADD COLUMN free_compile INTEGER NOT NULL DEFAULT 0")
            if "runtime_profile" not in {row[1] for row in con.execute("PRAGMA table_info(submission_receipts)")}:
                con.execute("ALTER TABLE submission_receipts ADD COLUMN runtime_profile TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS entries_user_status_idx ON entries(user_id,status,tournament_id)")
            con.execute("CREATE INDEX IF NOT EXISTS entries_tournament_status_idx ON entries(tournament_id,status)")
            con.execute("CREATE INDEX IF NOT EXISTS tournaments_coding_deadline_idx ON tournaments(status,coding_deadline)")
            con.execute("CREATE INDEX IF NOT EXISTS matches_claim_order_idx ON matches(leg_no,round_no,pairing_no,tournament_id,id) WHERE status='SCHEDULED'")
            con.execute("CREATE INDEX IF NOT EXISTS matches_tournament_status_idx ON matches(tournament_id,status)")
            con.execute("CREATE INDEX IF NOT EXISTS matches_tournament_schedule_idx ON matches(tournament_id,leg_no,round_no,pairing_no)")
            con.execute("CREATE INDEX IF NOT EXISTS matches_white_entry_updated_idx ON matches(white_entry_id,updated_at DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS matches_black_entry_updated_idx ON matches(black_entry_id,updated_at DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS matches_finished_schedule_idx ON matches(tournament_id,leg_no,round_no,pairing_no) WHERE status='FINISHED'")
            con.execute("CREATE INDEX IF NOT EXISTS matches_white_entry_result_idx ON matches(white_entry_id,status,result)")
            con.execute("CREATE INDEX IF NOT EXISTS matches_black_entry_result_idx ON matches(black_entry_id,status,result)")
            con.execute("CREATE INDEX IF NOT EXISTS matches_running_lease_idx ON matches(COALESCE(lease_expires_at,0)) WHERE status='RUNNING'")
            con.execute("CREATE INDEX IF NOT EXISTS matches_running_order_idx ON matches(leg_no,round_no,pairing_no) WHERE status='RUNNING'")
            con.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))

    # ---------------- users & sessions ----------------
    def create_user(self, username: str, password: str, role: str = "CONTESTANT") -> dict[str, Any]:
        username = username.lower()
        with self.tx() as con:
            try:
                cur = con.execute(
                    "INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)",
                    (username, _hash_password(password), role, now()),
                )
            except sqlite3.IntegrityError:
                raise StoreError(409, "USERNAME_TAKEN", "Username already exists.") from None
            return {"id": cur.lastrowid, "username": username, "role": role}

    def verify_login(self, username: str, password: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute("SELECT * FROM users WHERE username = ?", (username.lower(),)).fetchone()
        # verify against a dummy hash when the user is unknown to keep timing similar
        encoded = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        ok = _verify_password(password, encoded)
        if not row or not ok:
            return None
        return {"id": row["id"], "username": row["username"], "role": row["role"]}

    def set_role(self, username: str, role: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE users SET role = ? WHERE username = ?", (role, username.lower()))

    def create_session(self, user_id: int) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.tx() as con:
            con.execute(
                "INSERT INTO sessions(token_hash, user_id, csrf_token, expires_at) VALUES (?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, now() + SESSION_TTL),
            )
        return token, csrf

    def get_session(self, token: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute(
                "SELECT u.id, u.username, u.role, s.csrf_token FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires_at > ?",
                (hashlib.sha256(token.encode()).hexdigest(), now()),
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, token: str) -> None:
        with self.tx() as con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute("SELECT id, username, role, created_at FROM users WHERE username = ?", (username.lower(),)).fetchone()
        return dict(row) if row else None

    def audit(self, actor_id: int | None, action: str, target: str | None = None) -> None:
        with self.tx() as con:
            con.execute("INSERT INTO audit_log(actor_id, action, target, created_at) VALUES (?,?,?,?)", (actor_id, action, target, now()))

    # ---------------- tournaments ----------------
    def create_tournament(self, name: str, clock_seconds: int, created_by: int, coding_seconds: int = 3600) -> dict[str, Any]:
        tid = str(uuid.uuid4())
        with self.tx() as con:
            con.execute(
                "INSERT INTO tournaments(id,name,game_id,game_version,clock_seconds,status,created_by,created_at,coding_seconds) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (tid, name, "ott", "1", clock_seconds, "DRAFT", created_by, now(), coding_seconds),
            )
        HUB.publish("tournament", tid)
        return self.get_tournament(tid)  # type: ignore[return-value]

    _TOUR_SQL = (
        "SELECT t.*, "
        "(SELECT COUNT(*) FROM entries e WHERE e.tournament_id=t.id AND e.status<>'REMOVED') AS entries_count, "
        "(SELECT COUNT(*) FROM entries e WHERE e.tournament_id=t.id AND e.status='READY') AS ready_count, "
        "(SELECT COUNT(*) FROM matches m WHERE m.tournament_id=t.id) AS matches_total, "
        "(SELECT COUNT(*) FROM matches m WHERE m.tournament_id=t.id AND m.status='FINISHED') AS matches_finished, "
        "(SELECT COUNT(*) FROM matches m WHERE m.tournament_id=t.id AND m.status IN ('RUNNING','PAUSED')) AS matches_running "
        "FROM tournaments t "
    )
    _TOUR_LIST_SQL = (
        "SELECT t.*, COALESCE(e.entries_count,0) AS entries_count, COALESCE(e.ready_count,0) AS ready_count, "
        "COALESCE(m.matches_total,0) AS matches_total, COALESCE(m.matches_finished,0) AS matches_finished, "
        "COALESCE(m.matches_running,0) AS matches_running FROM tournaments t "
        "LEFT JOIN (SELECT tournament_id, SUM(CASE WHEN status<>'REMOVED' THEN 1 ELSE 0 END) AS entries_count, "
        "SUM(CASE WHEN status='READY' THEN 1 ELSE 0 END) AS ready_count FROM entries GROUP BY tournament_id) e "
        "ON e.tournament_id=t.id "
        "LEFT JOIN (SELECT tournament_id, COUNT(*) AS matches_total, "
        "SUM(CASE WHEN status='FINISHED' THEN 1 ELSE 0 END) AS matches_finished, "
        "SUM(CASE WHEN status IN ('RUNNING','PAUSED') THEN 1 ELSE 0 END) AS matches_running "
        "FROM matches GROUP BY tournament_id) m ON m.tournament_id=t.id "
    )

    def get_tournament(self, tid: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute(self._TOUR_SQL + "WHERE t.id = ?", (tid,)).fetchone()
        return dict(row) if row else None

    def tournament_exists(self, tid: str) -> bool:
        with self.tx() as con:
            return con.execute("SELECT 1 FROM tournaments WHERE id = ?", (tid,)).fetchone() is not None

    def get_tournament_status(self, tid: str) -> str | None:
        with self.tx() as con:
            row = con.execute("SELECT status FROM tournaments WHERE id = ?", (tid,)).fetchone()
        return row["status"] if row else None

    def list_tournaments(self) -> list[dict[str, Any]]:
        with self.tx() as con:
            rows = con.execute(self._TOUR_LIST_SQL + "ORDER BY t.created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_tournament(self, tid: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as con:
            con.execute(f"UPDATE tournaments SET {cols} WHERE id = ?", (*fields.values(), tid))
        HUB.publish("tournament", tid)

    def transition(self, tid: str, allowed: tuple[str, ...], new_status: str, **extra: Any) -> bool:
        sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in extra])
        marks = ",".join("?" for _ in allowed)
        with self.tx() as con:
            cur = con.execute(
                f"UPDATE tournaments SET {sets} WHERE id = ? AND status IN ({marks})",
                (new_status, *extra.values(), tid, *allowed),
            )
            changed = cur.rowcount == 1
            if changed and new_status == "CANCELLED":
                con.execute("UPDATE submission_receipts SET status='FAILED',reason_code='OPERATOR_CANCELLED' WHERE tournament_id=? AND status IN ('QUEUED','REVIEW')",(tid,))
        if changed:
            HUB.publish("tournament", tid, status=new_status)
        return changed

    # ---------------- entries & bots ----------------
    def create_entry(self, tid: str, user_id: int) -> dict[str, Any]:
        eid = str(uuid.uuid4())
        with self.tx() as con:
            try:
                con.execute(
                    "INSERT INTO entries(id,tournament_id,user_id,status,registered_at) VALUES (?,?,?,?,?)",
                    (eid, tid, user_id, "REGISTERED", now()),
                )
            except sqlite3.IntegrityError:
                raise StoreError(409, "ENTRY_EXISTS", "This account already has an entry in the tournament.") from None
        HUB.publish("entry", tid)
        return self.get_entry(eid)  # type: ignore[return-value]

    _ENTRY_SELECT = (
        "SELECT e.id, e.tournament_id, e.user_id, e.status, e.registered_at, e.bot_submission_id, u.username, "
        "e.submitted_at, e.submit_note, e.draft_updated_at, e.compile_count, e.last_ok_sha, "
        "b.compile_status, b.created_at AS bot_created_at, b.source_size "
    )
    _ENTRY_FROM = (
        "FROM entries e JOIN users u ON u.id = e.user_id LEFT JOIN bot_submissions b ON b.id = e.bot_submission_id "
    )
    _ENTRY_PENDING_STATUS = (
        ", (SELECT r.status FROM submission_receipts r WHERE r.entry_id=e.id "
        "AND r.status IN ('QUEUED','REVIEW') ORDER BY r.created_at,r.id LIMIT 1) AS submission_status "
    )
    _ENTRY_SQL = _ENTRY_SELECT + _ENTRY_FROM
    _ENTRY_WITH_SUBMISSION_SQL = (
        _ENTRY_SELECT + _ENTRY_PENDING_STATUS + _ENTRY_FROM
    )

    def get_entry(self, eid: str, *, include_submission_status: bool = False) -> dict[str, Any] | None:
        projection = self._ENTRY_WITH_SUBMISSION_SQL if include_submission_status else self._ENTRY_SQL
        with self.tx() as con:
            row = con.execute(projection + "WHERE e.id = ?", (eid,)).fetchone()
        return dict(row) if row else None

    def get_user_entry(self, tid: str, user_id: int, *, include_submission_status: bool = False) -> dict[str, Any] | None:
        projection = self._ENTRY_WITH_SUBMISSION_SQL if include_submission_status else self._ENTRY_SQL
        with self.tx() as con:
            row = con.execute(projection + "WHERE e.tournament_id = ? AND e.user_id = ?", (tid, user_id)).fetchone()
        return dict(row) if row else None

    def list_entries(self, tid: str, include_removed: bool = False, *,
                     include_submission_status: bool = False) -> list[dict[str, Any]]:
        extra = "" if include_removed else "AND e.status <> 'REMOVED' "
        projection = self._ENTRY_WITH_SUBMISSION_SQL if include_submission_status else self._ENTRY_SQL
        with self.tx() as con:
            rows = con.execute(
                projection + f"WHERE e.tournament_id = ? {extra}ORDER BY e.registered_at, e.id", (tid,)
            ).fetchall()
        return [dict(r) for r in rows]

    def user_entries(self, user_id: int, *, include_submission_status: bool = False) -> list[dict[str, Any]]:
        projection = self._ENTRY_WITH_SUBMISSION_SQL if include_submission_status else self._ENTRY_SQL
        with self.tx() as con:
            rows = con.execute(projection + "WHERE e.user_id = ? AND e.status <> 'REMOVED'", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def iter_user_standings_inputs(self, user_id: int, *, finished_matches_only: bool = False,
                                   stream_matches: bool = False) -> Iterator[dict[str, Any]]:
        """Yield each tournament's roster and results from three queries in one read snapshot."""
        with self.tx() as con:
            con.execute("BEGIN")
            tournaments = con.execute(
                "SELECT mine.id AS entry_id,mine.tournament_id,t.name AS tournament_name,t.status,b.compile_status "
                "FROM entries mine JOIN tournaments t ON t.id=mine.tournament_id "
                "LEFT JOIN bot_submissions b ON b.id=mine.bot_submission_id "
                "WHERE mine.user_id=? AND mine.status<>'REMOVED' ORDER BY mine.status,mine.tournament_id",
                (user_id,),
            ).fetchall()
            if not tournaments:
                return
            roster_cursor = con.execute(
                "SELECT e.id,e.tournament_id,u.username FROM entries mine "
                "JOIN entries e ON e.tournament_id=mine.tournament_id JOIN users u ON u.id=e.user_id "
                "WHERE mine.user_id=? AND mine.status<>'REMOVED' AND e.status<>'REMOVED' "
                "ORDER BY mine.status,mine.tournament_id,e.registered_at,e.id",
                (user_id,),
            )
            finished_filter = " AND m.status='FINISHED'" if finished_matches_only else ""
            match_count_column = (
                ",COUNT(*) OVER (PARTITION BY m.tournament_id) AS tournament_match_count"
                if stream_matches else ""
            )
            results_cursor = con.execute(
                "SELECT m.id,m.tournament_id,m.round_no,m.leg_no,m.pairing_no,m.white_entry_id,m.black_entry_id,m.status,m.result "
                + match_count_column + " FROM entries mine JOIN matches m ON m.tournament_id=mine.tournament_id "
                "WHERE mine.user_id=? AND mine.status<>'REMOVED'" + finished_filter + " "
                "ORDER BY mine.status,mine.tournament_id,m.leg_no,m.round_no,m.pairing_no",
                (user_id,),
            )
            rosters = groupby(roster_cursor, key=lambda row: row["tournament_id"])
            results = groupby(results_cursor, key=lambda row: row["tournament_id"])
            roster_group = next(rosters, (None, ()))
            result_group = next(results, (None, ()))
            for tournament in tournaments:
                tid = tournament["tournament_id"]
                entries, matches = [], []
                matches_count = 0
                advance_results_after_yield = False
                if roster_group[0] == tid:
                    entries = [dict(row) for row in roster_group[1]]
                    roster_group = next(rosters, (None, ()))
                if result_group[0] == tid:
                    if stream_matches:
                        match_rows = result_group[1]
                        first_match = next(match_rows, None)
                        if first_match is not None:
                            matches_count = first_match["tournament_match_count"]
                            matches = chain((first_match,), match_rows)
                        advance_results_after_yield = True
                    else:
                        matches = [dict(row) for row in result_group[1]]
                        result_group = next(results, (None, ()))
                result = dict(tournament) | {"entries": entries, "matches": matches}
                if stream_matches:
                    result["matches_count"] = matches_count
                yield result
                if advance_results_after_yield:
                    result_group = next(results, (None, ()))

    def remove_entry(self, entry_id: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE entries SET status = 'REMOVED', bot_submission_id = NULL WHERE id = ?", (entry_id,))
            con.execute("UPDATE submission_receipts SET status='FAILED',reason_code='ENTRY_REMOVED' WHERE entry_id=? AND status IN ('QUEUED','REVIEW')",(entry_id,))
            row = con.execute("SELECT tournament_id FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row:
            HUB.publish("entry", row["tournament_id"])

    def get_draft(self, entry_id: str, *, with_language: bool = False
                  ) -> tuple[str | None, int | None] | tuple[str | None, int | None, str]:
        with self.tx() as con:
            row = con.execute("SELECT draft_source, draft_updated_at, draft_language FROM entries WHERE id = ?", (entry_id,)).fetchone()
        draft = (row["draft_source"], row["draft_updated_at"]) if row else (None, None)
        return (*draft, row["draft_language"] if row else "cpp17") if with_language else draft

    def save_draft(self, entry_id: str, source: str, language: str = "cpp17") -> int:
        language_profile(language)
        stamp = now()
        with self.tx() as con:
            cur = con.execute(
                "UPDATE entries SET draft_source = ?, draft_updated_at = ?, draft_language = ?, submit_note=NULL WHERE id = ? AND submitted_at IS NULL AND NOT EXISTS (SELECT 1 FROM submission_receipts r WHERE r.entry_id=entries.id AND r.status IN ('QUEUED','REVIEW'))",
                (source, stamp, language, entry_id),
            )
            if cur.rowcount != 1:
                raise StoreError(409, "SUBMISSION_LOCKED", "A final submission is already accepted and its source cannot be changed.")
        return stamp

    def get_pending_submission(self, entry_id: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute("SELECT * FROM submission_receipts WHERE entry_id=? AND status IN ('QUEUED','REVIEW') ORDER BY created_at,id LIMIT 1",(entry_id,)).fetchone()
        return dict(row) if row else None

    def reserve_submission(self, entry_id: str, tid: str, user_id: int, source: str,
                           received_at: float, *, auto: bool = False,
                           runtime_profile: dict[str, Any] | None = None, language: str = "cpp17") -> dict[str, Any]:
        language_profile(language)
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT e.*,t.status AS tournament_status,t.coding_deadline,t.coding_started_at FROM entries e JOIN tournaments t ON t.id=e.tournament_id WHERE e.id=? AND e.tournament_id=? AND e.user_id=?",(entry_id,tid,user_id)).fetchone()
            if not row or row["status"] == "REMOVED":
                raise StoreError(409,"NO_ENTRY","The entry does not belong to this contestant.")
            if not auto:
                try:
                    repeated_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
                except (AttributeError,UnicodeError):
                    raise StoreError(400,"BAD_SOURCE","Source must be valid UTF-8.") from None
                previous = con.execute("SELECT * FROM submission_receipts WHERE entry_id=? AND source_hash=? AND language=? AND status IN ('QUEUED','REVIEW','READY') ORDER BY created_at DESC,id DESC LIMIT 1",(entry_id,repeated_hash,language)).fetchone()
                if previous and (previous["status"] != "READY" or row["bot_submission_id"] == previous["bot_id"]):
                    # Replaying an acknowledgement does not admit a late source.
                    return dict(previous)
            if row["submitted_at"] is not None:
                raise StoreError(409,"ALREADY_SUBMITTED","The entry already has a final submission.")
            if row["tournament_status"] != "CODING":
                raise StoreError(409,"NOT_CODING","The coding phase is not open.")
            deadline = row["coding_deadline"]
            if not deadline:
                raise StoreError(409,"DEADLINE_UNSET","The coding deadline is not configured.")
            if auto:
                if self._db_now(con) < deadline:
                    raise StoreError(409,"CODING_OPEN","Automatic submission starts after the coding deadline.")
                source = row["draft_source"] or ""
                language = row["draft_language"]
                language_profile(language)
                received_at = float(deadline)
            elif (isinstance(received_at,bool) or not isinstance(received_at,(int,float))
                  or not math.isfinite(received_at) or received_at >= deadline
                  or received_at > time.time()
                  or received_at < (row["coding_started_at"] or 0)):
                raise StoreError(409,"DEADLINE_PASSED","The complete submission was not received during the coding window.")
            try:
                raw = source.encode("utf-8")
            except (AttributeError,UnicodeError):
                raise StoreError(400,"BAD_SOURCE","Source must be valid UTF-8.") from None
            if not source.strip() or len(raw) > 256*1024 or b"\x00" in raw:
                raise StoreError(400,"BAD_SOURCE","Source must be nonempty, at most 256 KiB and contain no NUL.")
            digest = hashlib.sha256(raw).hexdigest()
            pending = con.execute("SELECT * FROM submission_receipts WHERE entry_id=? AND status IN ('QUEUED','REVIEW') ORDER BY created_at,id LIMIT 1",(entry_id,)).fetchone()
            if pending:
                if pending["source_hash"] == digest and pending["language"] == language:
                    return dict(pending)
                raise StoreError(409,"SUBMISSION_PENDING","An immutable final source was already accepted.")
            if auto and con.execute("SELECT 1 FROM submission_receipts WHERE entry_id=? AND (received_at>=? OR (status='FAILED' AND source_hash=? AND language=?)) LIMIT 1",
                                    (entry_id,deadline,digest,language)).fetchone():
                raise StoreError(409,"AUTO_ALREADY_ATTEMPTED","Automatic submission already attempted this source or coding deadline.")
            if row["compile_count"] >= 10 and row["last_ok_sha"] != build_key(digest, language):
                raise StoreError(429,"COMPILE_LIMIT","No compile attempts remain for this source.")
            if con.execute("SELECT COUNT(*) FROM submission_receipts WHERE status='QUEUED'").fetchone()[0] >= 30:
                raise StoreError(429,"SUBMISSION_QUEUE_FULL","The final submission queue is full; retry later.")
            receipt_id = str(uuid.uuid4())
            stamp = self._db_now(con)
            con.execute("INSERT INTO submission_receipts(id,bot_id,entry_id,tournament_id,user_id,source,source_hash,received_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,'QUEUED',?)",
                        (receipt_id,receipt_id,entry_id,tid,user_id,source,digest,received_at,stamp))
            con.execute("UPDATE submission_receipts SET language=? WHERE id=?", (language,receipt_id))
            con.execute("UPDATE submission_receipts SET free_compile=? WHERE id=?",(int(row["last_ok_sha"]==build_key(digest, language)),receipt_id))
            con.execute("UPDATE submission_receipts SET runtime_profile=? WHERE id=?",
                        (json.dumps(runtime_profile,sort_keys=True,separators=(",",":"),allow_nan=False) if runtime_profile is not None else None,receipt_id))
            con.execute("UPDATE entries SET draft_source=?,draft_updated_at=?,draft_language=?,submit_note=NULL WHERE id=?",(source,int(received_at),language,entry_id))
            result = con.execute("SELECT * FROM submission_receipts WHERE id=?",(receipt_id,)).fetchone()
        HUB.publish("entry",tid)
        return dict(result)

    def queued_submissions(self, limit: int = 30) -> list[dict[str, Any]]:
        limit = min(30,max(1,int(limit)))
        with self.tx() as con:
            rows = con.execute("SELECT r.* FROM submission_receipts r JOIN entries e ON e.id=r.entry_id JOIN tournaments t ON t.id=r.tournament_id WHERE r.status='QUEUED' AND e.submitted_at IS NULL AND e.status<>'REMOVED' AND t.status='CODING' ORDER BY r.received_at,r.id LIMIT ?",(limit,)).fetchall()
        return [dict(row) for row in rows]

    def fail_submission(self, receipt_id: str, reason: str, *, infra: bool = False,
                        compile_lease: tuple[str,str]) -> None:
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            self._assert_compile_slot(con,*compile_lease)
            row = con.execute("SELECT * FROM submission_receipts WHERE id=?",(receipt_id,)).fetchone()
            if not row or row["status"] != "QUEUED":
                return
            attempts = row["infra_attempts"] + (1 if infra else 0)
            status = "REVIEW" if infra and attempts >= MAX_ATTEMPTS else "QUEUED" if infra else "FAILED"
            con.execute("UPDATE submission_receipts SET status=?,infra_attempts=?,reason_code=? WHERE id=?",
                        (status,attempts,reason[:200],receipt_id))
            con.execute("UPDATE entries SET submit_note=? WHERE id=? AND submitted_at IS NULL",(reason[:200],row["entry_id"]))
        HUB.publish("entry",row["tournament_id"])

    def finalize_entry(self, entry_id: str, tid: str, user_id: int, bot_id: str, source: str, received_at: int | None = None,
                       compile_lease: tuple[str, str] | None = None, language: str = "cpp17") -> bool:
        """Atomically create the single, immutable submission. False if the entry was already submitted."""
        language_profile(language)
        raw = source.encode("utf-8")
        stamp = now() if received_at is None else received_at
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            if compile_lease is not None:
                self._assert_compile_slot(con,*compile_lease)
            receipt = con.execute("SELECT * FROM submission_receipts WHERE bot_id=?",(bot_id,)).fetchone()
            if receipt:
                if (receipt["entry_id"] != entry_id or receipt["tournament_id"] != tid or receipt["user_id"] != user_id
                        or receipt["source_hash"] != hashlib.sha256(raw).hexdigest() or receipt["language"] != language):
                    raise StoreError(409,"RECEIPT_CONFLICT","Final source differs from the accepted receipt.")
                stamp = receipt["received_at"]
                if receipt["status"] not in ("QUEUED","READY"):
                    raise StoreError(409,"RECEIPT_CLOSED","This receipt is not eligible for finalization.")
            elif con.execute("SELECT 1 FROM submission_receipts WHERE entry_id=? AND status IN ('QUEUED','REVIEW')",(entry_id,)).fetchone():
                raise StoreError(409,"RECEIPT_REQUIRED","Finalization must use the accepted receipt.")
            row = con.execute("SELECT submitted_at,status,bot_submission_id FROM entries WHERE id = ? AND tournament_id=? AND user_id=?", (entry_id,tid,user_id)).fetchone()
            if row and receipt and receipt["status"] == "READY" and row["bot_submission_id"] == bot_id:
                return True
            if not row or row["submitted_at"] is not None or row["status"] == "REMOVED":
                return False
            con.execute(
                "INSERT INTO bot_submissions(id,tournament_id,user_id,language,source,source_sha256,source_size,compile_status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (bot_id, tid, user_id, language, source, hashlib.sha256(raw).hexdigest(), len(raw), "READY", stamp),
            )
            con.execute(
                "UPDATE entries SET bot_submission_id = ?, status = 'READY', submitted_at = ?, draft_source = ?, draft_updated_at = ?, draft_language = ?, submit_note = NULL WHERE id = ?",
                (bot_id, stamp, source, stamp, language, entry_id),
            )
            if receipt:
                con.execute("UPDATE submission_receipts SET status='READY',reason_code=NULL WHERE id=?",(receipt["id"],))
                con.execute("INSERT INTO audit_log(actor_id,action,target,created_at) VALUES(NULL,'code.receipt_ready',?,?)",
                            (receipt["id"],self._db_now(con)))
        HUB.publish("entry", tid)
        return True

    def bump_compile(self, entry_id: str, limit: int, *, receipt_id: str | None = None) -> int:
        """Atomically consume one compile attempt; raise 429 when the limit is exhausted."""
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            pending = con.execute("SELECT id,status FROM submission_receipts WHERE entry_id=? AND status IN ('QUEUED','REVIEW') LIMIT 1",(entry_id,)).fetchone()
            if pending and (pending["id"] != receipt_id or pending["status"] != "QUEUED"):
                raise StoreError(409,"SUBMISSION_PENDING","A final submission already owns this entry's compile quota.")
            if receipt_id:
                receipt = con.execute("SELECT compile_charged FROM submission_receipts WHERE id=? AND entry_id=? AND status='QUEUED'",(receipt_id,entry_id)).fetchone()
                if not receipt:
                    raise StoreError(409,"RECEIPT_REQUIRED","A queued receipt is required for a final build.")
                if receipt["compile_charged"]:
                    return con.execute("SELECT compile_count FROM entries WHERE id=?",(entry_id,)).fetchone()[0]
            cur = con.execute(
                "UPDATE entries SET compile_count = compile_count + 1 WHERE id = ? AND compile_count < ? AND submitted_at IS NULL",
                (entry_id, limit),
            )
            if cur.rowcount != 1:
                raise StoreError(429, "COMPILE_LIMIT", f"You have used all {limit} compile attempts.")
            if receipt_id:
                con.execute("UPDATE submission_receipts SET compile_charged=1 WHERE id=?",(receipt_id,))
            return con.execute("SELECT compile_count FROM entries WHERE id = ?", (entry_id,)).fetchone()[0]

    def set_last_ok(self, entry_id: str, sha: str | None, compile_lease: tuple[str, str] | None = None) -> None:
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            if compile_lease is not None:
                self._assert_compile_slot(con,*compile_lease)
            con.execute("UPDATE entries SET last_ok_sha = ? WHERE id = ?", (sha, entry_id))

    def refund_compile(self, entry_id: str, *, receipt_id: str | None = None,
                       compile_lease: tuple[str,str] | None = None) -> None:
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            if compile_lease is not None:
                self._assert_compile_slot(con,*compile_lease)
            if receipt_id:
                receipt = con.execute("SELECT compile_charged FROM submission_receipts WHERE id=? AND entry_id=?",(receipt_id,entry_id)).fetchone()
                if not receipt or not receipt["compile_charged"]:
                    return
                con.execute("UPDATE submission_receipts SET compile_charged=0 WHERE id=?",(receipt_id,))
            con.execute("UPDATE entries SET compile_count=MAX(0,compile_count-1) WHERE id=?",(entry_id,))

    def set_submit_note(self, entry_id: str, note: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE entries SET submit_note = ? WHERE id = ? AND submitted_at IS NULL", (note, entry_id))
            row = con.execute("SELECT tournament_id FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row:
            HUB.publish("entry", row["tournament_id"])

    def expired_coding(self) -> list[dict[str, Any]]:
        with self.tx() as con:
            rows = con.execute(
                "SELECT t.id AS tid, e.id AS eid, e.user_id, e.draft_source, e.draft_language FROM tournaments t JOIN entries e ON e.tournament_id = t.id "
                "WHERE t.status = 'CODING' AND t.coding_deadline <= ? AND e.submitted_at IS NULL AND e.status <> 'REMOVED' AND e.submit_note IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM submission_receipts r WHERE r.entry_id=e.id AND (r.status IN ('QUEUED','REVIEW') OR r.received_at>=t.coding_deadline OR (r.status='FAILED' AND r.source=e.draft_source AND r.language=e.draft_language)))",
                (now(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_bot(self, bid: str, with_source: bool = False) -> dict[str, Any] | None:
        cols = "*" if with_source else "id,tournament_id,user_id,language,source_sha256,source_size,compile_status,compile_message,created_at"
        with self.tx() as con:
            row = con.execute(f"SELECT {cols} FROM bot_submissions WHERE id = ?", (bid,)).fetchone()
        return dict(row) if row else None

    # ---------------- matches ----------------
    def create_matches(self, tid: str, rows: Iterable[dict[str, Any]], clock_ms: int) -> None:
        self.create_matches_from_schedule(
            tid,
            ((row["round_no"], row["leg_no"], row["pairing_no"], row["white"], row["black"])
             for row in rows),
            clock_ms,
        )

    def create_matches_from_schedule(self, tid: str,
                                     rows: Iterable[tuple[int, int, int, str, str]],
                                     clock_ms: int) -> None:
        """Insert compact schedule rows directly into SQLite."""
        with self.tx() as con:
            if con.execute("SELECT 1 FROM matches WHERE tournament_id = ? LIMIT 1", (tid,)).fetchone():
                raise StoreError(409, "SCHEDULE_EXISTS", "Schedule already exists.")
            stamp = now()
            con.executemany(
                "INSERT INTO matches(id,tournament_id,round_no,leg_no,pairing_no,white_entry_id,black_entry_id,status,phase,"
                "white_clock_ms,black_clock_ms,updated_at,engine_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ((str(uuid.uuid4()), tid, round_no, leg_no, pairing_no, white, black,
                  "SCHEDULED", "PLACEMENT", clock_ms, clock_ms, stamp, ENGINE_VERSION)
                 for round_no, leg_no, pairing_no, white, black in rows),
            )
        HUB.publish("tournament", tid)

    _MATCH_FROM_SQL = (
        "FROM matches m JOIN tournaments t ON t.id = m.tournament_id "
        "JOIN entries we ON we.id = m.white_entry_id JOIN users wu ON wu.id = we.user_id "
        "JOIN entries be ON be.id = m.black_entry_id JOIN users bu ON bu.id = be.user_id "
    )
    _MATCH_SQL = (
        "SELECT m.*, t.name AS tournament_name, t.clock_seconds, t.game_id, t.game_version, "
        "wu.username AS white_name, bu.username AS black_name "
        + _MATCH_FROM_SQL
    )
    _MATCH_OVERVIEW_COLUMNS = (
        "m.id, m.tournament_id, m.round_no, m.leg_no, m.pairing_no, "
        "m.white_entry_id, m.black_entry_id, m.status, m.result, m.result_reason, "
        "m.phase, m.side_to_act, m.state_version, m.move_count, "
        "m.white_clock_ms, m.black_clock_ms, t.name AS tournament_name, t.clock_seconds, "
        "t.game_id, t.game_version, wu.username AS white_name, bu.username AS black_name "
    )
    _MATCH_OVERVIEW_SQL = "SELECT " + _MATCH_OVERVIEW_COLUMNS + _MATCH_FROM_SQL
    _MATCH_RENDER_SQL = "SELECT " + _MATCH_OVERVIEW_COLUMNS + ", m.state_json " + _MATCH_FROM_SQL
    _MATCH_SUMMARY_COLUMNS = (
        "m.id, m.tournament_id, m.round_no, m.leg_no, m.pairing_no, "
        "m.white_entry_id, m.black_entry_id, m.status, m.result, m.result_reason, "
        "m.phase, m.move_count, m.side_to_act, t.name AS tournament_name, "
        "wu.username AS white_name, bu.username AS black_name "
    )
    _MATCH_SUMMARY_SQL = "SELECT " + _MATCH_SUMMARY_COLUMNS + _MATCH_FROM_SQL

    def get_match(self, mid: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute(self._MATCH_SQL + "WHERE m.id = ?", (mid,)).fetchone()
        return dict(row) if row else None

    def get_match_overview(self, mid: str) -> dict[str, Any] | None:
        """Read match metadata without loading state or execution snapshots."""
        with self.tx() as con:
            row = con.execute(self._MATCH_OVERVIEW_SQL + "WHERE m.id = ?", (mid,)).fetchone()
        return dict(row) if row else None

    def get_match_render(self, mid: str) -> dict[str, Any] | None:
        """Read render fields and state, excluding execution snapshots."""
        with self.tx() as con:
            row = con.execute(self._MATCH_RENDER_SQL + "WHERE m.id = ?", (mid,)).fetchone()
        return dict(row) if row else None

    def get_match_renders(self, match_ids: list[str]) -> list[dict[str, Any]]:
        if not match_ids:
            return []
        marks = ",".join("?" for _ in match_ids)
        with self.tx() as con:
            rows = con.execute(self._MATCH_RENDER_SQL + f"WHERE m.id IN ({marks})", match_ids).fetchall()
        return [dict(row) for row in rows]

    def get_match_poll(self, mid: str) -> dict[str, Any] | None:
        """Read only live polling fields; avoid loading saved state and execution snapshot."""
        with self.tx() as con:
            row = con.execute(
                "SELECT id,status,result,result_reason,state_version,white_clock_ms,black_clock_ms,side_to_act "
                "FROM matches WHERE id = ?", (mid,),
            ).fetchone()
        return dict(row) if row else None

    def list_matches(self, tid: str | None = None, statuses: tuple[str, ...] | None = None, limit: int = 500,
                     *, include_state: bool = True) -> list[dict[str, Any]]:
        where, args = [], []
        if tid:
            where.append("m.tournament_id = ?")
            args.append(tid)
        if statuses == ("RUNNING",):
            # Match the partial index predicate and keep feed rows in schedule order.
            where.append("m.status = 'RUNNING'")
        elif statuses:
            where.append(f"m.status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        projection = self._MATCH_SQL if include_state else self._MATCH_OVERVIEW_SQL
        if statuses == ("RUNNING",):
            projection = projection.replace(
                "FROM matches m JOIN",
                "FROM matches m INDEXED BY matches_running_order_idx JOIN",
                1,
            )
        sql = projection + (f"WHERE {' AND '.join(where)} " if where else "")
        sql += "ORDER BY m.leg_no, m.round_no, m.pairing_no LIMIT ?"
        with self.tx() as con:
            rows = con.execute(sql, (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def list_match_summaries(self, tid: str, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """Read tournament schedule summaries without execution snapshots."""
        sql = self._MATCH_SUMMARY_SQL + "WHERE m.tournament_id = ? ORDER BY m.leg_no, m.round_no, m.pairing_no"
        args: tuple[Any, ...] = (tid,)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += (limit, offset)
        with self.tx() as con:
            rows = con.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def count_match_summaries(self, tid: str) -> int:
        with self.tx() as con:
            row = con.execute("SELECT COUNT(*) FROM matches WHERE tournament_id = ?", (tid,)).fetchone()
        return int(row[0])

    def list_recent_finished_match_summaries(self, tid: str, limit: int) -> list[dict[str, Any]]:
        """Read latest finished rows through the partial schedule index."""
        sql = self._MATCH_SUMMARY_SQL + (
            "WHERE m.tournament_id = ? AND m.status = 'FINISHED' "
            "ORDER BY m.leg_no DESC, m.round_no DESC, m.pairing_no DESC LIMIT ?"
        )
        with self.tx() as con:
            rows = con.execute(sql, (tid, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def list_match_results(self, tid: str) -> list[dict[str, Any]]:
        """Read only the complete results and schedule fields needed for standings."""
        with self.tx() as con:
            rows = con.execute(
                "SELECT id,tournament_id,round_no,leg_no,pairing_no,white_entry_id,black_entry_id,status,result "
                "FROM matches WHERE tournament_id=? ORDER BY leg_no,round_no,pairing_no",
                (tid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_finished_match_results(self, tid: str) -> list[dict[str, Any]]:
        """Read final results in schedule order for standings calculations."""
        with self.tx() as con:
            rows = con.execute(
                "SELECT id,tournament_id,round_no,leg_no,pairing_no,white_entry_id,black_entry_id,status,result "
                "FROM matches WHERE tournament_id=? AND status='FINISHED' ORDER BY leg_no,round_no,pairing_no",
                (tid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_finished_match_results(self, tid: str) -> int:
        with self.tx() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM matches WHERE tournament_id=? AND status='FINISHED'", (tid,)
            ).fetchone()
        return int(row[0])

    def iter_finished_match_results(self, tid: str) -> Iterator[sqlite3.Row]:
        """Stream final results in schedule order for standings calculations."""
        with self.tx() as con:
            con.execute("BEGIN")
            yield from con.execute(
                "SELECT id,tournament_id,round_no,leg_no,pairing_no,white_entry_id,black_entry_id,status,result "
                "FROM matches WHERE tournament_id=? AND status='FINISHED' ORDER BY leg_no,round_no,pairing_no",
                (tid,),
            )

    def user_matches(self, user_id: int, limit: int = 30, *, include_state: bool = True) -> list[dict[str, Any]]:
        projection = self._MATCH_SQL if include_state else "SELECT m.updated_at, " + self._MATCH_SUMMARY_COLUMNS + self._MATCH_FROM_SQL
        sql = (
            "SELECT * FROM ("
            + projection + "WHERE we.user_id = ? UNION ALL "
            + projection + "WHERE be.user_id = ?"
            + ") ORDER BY updated_at DESC LIMIT ?"
        )
        with self.tx() as con:
            rows = con.execute(sql, (user_id, user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def user_match_totals(self, user_id: int) -> dict[str, int]:
        """Aggregate every finished match, including entries removed from a roster."""
        with self.tx() as con:
            row = con.execute(
                "SELECT COALESCE(SUM(points=3),0) AS wins, COALESCE(SUM(points=1),0) AS draws, "
                "COALESCE(SUM(points=0),0) AS losses, COALESCE(SUM(points),0) AS points FROM ("
                "SELECT CASE m.result WHEN 'WHITE_WIN' THEN 3 WHEN 'DRAW' THEN 1 ELSE 0 END AS points "
                "FROM matches m WHERE m.white_entry_id IN (SELECT id FROM entries WHERE user_id=?) AND m.status='FINISHED' "
                "UNION ALL "
                "SELECT CASE m.result WHEN 'BLACK_WIN' THEN 3 WHEN 'DRAW' THEN 1 ELSE 0 END AS points "
                "FROM matches m WHERE m.black_entry_id IN (SELECT id FROM entries WHERE user_id=?) AND m.status='FINISHED'"
                ")",
                (user_id, user_id),
            ).fetchone()
        return dict(row)

    @staticmethod
    def _db_now(con: sqlite3.Connection) -> int:
        return int(con.execute("SELECT CAST(strftime('%s','now') AS INTEGER)").fetchone()[0])

    @staticmethod
    def _owned(con: sqlite3.Connection, mid: str, lease: Lease | None) -> sqlite3.Row:
        if lease is None:
            raise LeaseLost("Execution lease is required.")
        row = con.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
        if (not row or row["status"] != "RUNNING" or row["lease_owner"] != lease.owner
                or row["lease_epoch"] != lease.epoch
                or (row["lease_expires_at"] or 0) <= Store._db_now(con)):
            raise LeaseLost("Execution lease expired or was replaced.")
        return row

    @staticmethod
    def _owned_action(con: sqlite3.Connection, mid: str, lease: Lease | None) -> sqlite3.Row:
        if lease is None:
            raise LeaseLost("Execution lease is required.")
        row = con.execute(
            "SELECT last_event_seq,white_clock_ms,black_clock_ms,tournament_id FROM matches "
            "WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_epoch=? "
            "AND lease_expires_at>CAST(strftime('%s','now') AS INTEGER)",
            (mid, lease.owner, lease.epoch),
        ).fetchone()
        if row is None:
            raise LeaseLost("Execution lease expired or was replaced.")
        return row

    @staticmethod
    def _release(con: sqlite3.Connection, mid: str, epoch: int, reason: str, stamp: int | None = None) -> None:
        con.execute("DELETE FROM entry_leases WHERE match_id=? AND epoch=?", (mid, epoch))
        con.execute("UPDATE match_attempts SET finished_at=?,reason=? WHERE match_id=? AND epoch=? AND finished_at IS NULL",
                    (stamp if stamp is not None else Store._db_now(con), reason, mid, epoch))
        con.execute("UPDATE matches SET lease_owner=NULL,lease_expires_at=NULL WHERE id=? AND lease_epoch=?", (mid, epoch))

    _CLAIM_SQL = (
        "SELECT m.*, t.status AS tournament_status, t.game_id, t.game_version, t.clock_seconds "
        "FROM matches m JOIN tournaments t ON t.id=m.tournament_id "
    )
    _NEXT_MATCH_FROM = (
        "FROM (SELECT m.* FROM matches m INDEXED BY matches_claim_order_idx "
        "WHERE m.status='SCHEDULED' AND m.tournament_id IN (SELECT id FROM tournaments WHERE status='RUNNING') "
        "AND m.available_at<=? AND m.attempt_count<? "
        "AND NOT EXISTS (SELECT 1 FROM entry_leases l WHERE l.entry_id IN (m.white_entry_id,m.black_entry_id)) "
        "AND (m.execution_snapshot IS NOT NULL OR (SELECT COUNT(*) FROM entries e "
        "JOIN bot_submissions b ON b.id=e.bot_submission_id WHERE e.id IN (m.white_entry_id,m.black_entry_id))=2) "
        "ORDER BY m.leg_no,m.round_no,m.pairing_no,m.tournament_id,m.id LIMIT 1) m "
        "JOIN tournaments t ON t.id=m.tournament_id "
    )
    _NEXT_MATCH_SQL = "SELECT m.*,t.status AS tournament_status,t.game_id,t.game_version,t.clock_seconds " + _NEXT_MATCH_FROM
    _NEXT_MATCH_EXISTS_SQL = "SELECT 1 " + _NEXT_MATCH_FROM

    def _claim_row(self, con: sqlite3.Connection, row: sqlite3.Row, owner: str, stamp: int) -> Lease | None:
        mid = row["id"]
        epoch = row["lease_epoch"] + 1
        snapshot = row["execution_snapshot"]
        if snapshot is None:
            bots = con.execute("SELECT e.id,e.bot_submission_id,b.source_sha256,b.language FROM entries e JOIN bot_submissions b ON b.id=e.bot_submission_id WHERE e.id IN (?,?) ORDER BY e.id",
                               (row["white_entry_id"], row["black_entry_id"])).fetchall()
            if len(bots) != 2:
                return None
            snapshot = json.dumps({"game_id":row["game_id"],"game_version":row["game_version"],
                "engine_version":row["engine_version"],"clock_seconds":row["clock_seconds"],
                "entries": [dict(b) for b in bots]}, sort_keys=True, separators=(",", ":"))
        con.execute("UPDATE matches SET status='RUNNING',lease_owner=?,lease_epoch=?,lease_expires_at=?,attempt_count=attempt_count+1,execution_snapshot=?,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",
                    (owner, epoch, stamp + LEASE_SECONDS, snapshot, stamp, stamp, mid))
        con.execute("INSERT INTO match_attempts(match_id,epoch,owner,started_at) VALUES(?,?,?,?)", (mid,epoch,owner,stamp))
        for eid in sorted((row["white_entry_id"], row["black_entry_id"])):
            con.execute("INSERT INTO entry_leases(entry_id,match_id,epoch) VALUES(?,?,?)", (eid,mid,epoch))
        return Lease(owner,epoch)

    def claim_match(self, mid: str, owner: str, max_running: int = 1) -> Lease | None:
        if not owner or max_running < 1:
            raise ValueError("Worker owner and positive concurrency are required.")
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            stamp = self._db_now(con)
            row = con.execute(self._CLAIM_SQL + "WHERE m.id=?", (mid,)).fetchone()
            if (not row or row["status"] != "SCHEDULED" or row["tournament_status"] != "RUNNING"
                    or row["available_at"] > stamp or row["attempt_count"] >= MAX_ATTEMPTS):
                return None
            if con.execute("SELECT COUNT(*) FROM matches WHERE status='RUNNING'").fetchone()[0] >= max_running:
                return None
            if con.execute("SELECT 1 FROM entry_leases WHERE entry_id IN (?,?)", (row["white_entry_id"], row["black_entry_id"])).fetchone():
                return None
            lease = self._claim_row(con, row, owner, stamp)
            if lease is None:
                return None
        HUB.publish("match",mid,tid=row["tournament_id"],status="RUNNING")
        return lease

    def claim_next_match(self, owner: str, max_running: int = 1) -> tuple[dict[str, Any], Lease] | None:
        """Select and claim an eligible match atomically, without a truncated queue scan."""
        if not owner or max_running < 1:
            raise ValueError("Worker owner and positive concurrency are required.")
        with self.tx() as con:
            stamp = self._db_now(con)
            if con.execute("SELECT COUNT(*) FROM matches WHERE status='RUNNING'").fetchone()[0] >= max_running:
                return None
            if con.execute(self._NEXT_MATCH_EXISTS_SQL, (stamp, MAX_ATTEMPTS)).fetchone() is None:
                return None
            con.execute("BEGIN IMMEDIATE")
            stamp = self._db_now(con)
            if con.execute("SELECT COUNT(*) FROM matches WHERE status='RUNNING'").fetchone()[0] >= max_running:
                return None
            row = con.execute(self._NEXT_MATCH_SQL, (stamp, MAX_ATTEMPTS)).fetchone()
            if row is None:
                return None
            lease = self._claim_row(con, row, owner, stamp)
            if lease is None:
                return None
            match = {key: row[key] for key in ("id", "tournament_id", "white_entry_id", "black_entry_id")}
        HUB.publish("match",match["id"],tid=match["tournament_id"],status="RUNNING")
        return match, lease

    def heartbeat(self, mid: str, lease: Lease) -> None:
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            self._owned(con,mid,lease)
            con.execute("UPDATE matches SET lease_expires_at=? WHERE id=?", (self._db_now(con)+LEASE_SECONDS,mid))

    def start_execution(self, mid: str, lease: Lease, runtime: dict[str, Any]) -> None:
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._owned(con,mid,lease)
            if row["execution_started"]:
                raise StoreError(409,"RECOVERY_REVIEW","Persistent bot memory cannot be restored automatically.")
            snapshot = json.loads(row["execution_snapshot"])
            previous = snapshot.get("runtime")
            if previous is not None and previous != runtime:
                raise StoreError(409,"RUNTIME_CHANGED","Execution runtime differs from the immutable snapshot.")
            snapshot["runtime"] = runtime
            con.execute("UPDATE matches SET execution_started=1,execution_snapshot=? WHERE id=?",
                        (json.dumps(snapshot,sort_keys=True,separators=(",",":")),mid))

    def checkpoint(self, mid: str, white_ms: int, black_ms: int, *, lease: Lease) -> None:
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._owned(con,mid,lease)
            if white_ms > row["white_clock_ms"] or black_ms > row["black_clock_ms"]:
                raise StoreError(409,"CLOCK_CONFLICT","Checkpoint cannot increase a clock.")
            con.execute("UPDATE matches SET white_clock_ms=?,black_clock_ms=?,clock_checkpoint_at=?,updated_at=? WHERE id=?",
                        (max(0,int(white_ms)),max(0,int(black_ms)),self._db_now(con),self._db_now(con),mid))

    def record_action(self, mid: str, seq: int, state: dict[str, Any], action: dict[str, Any], side: str, phase: str, clocks: dict[str, int], *, lease: Lease) -> None:
        encoded = json.dumps(action,sort_keys=True,separators=(",",":"))
        serialized = json.dumps(state,separators=(",",":"))
        serialized_bytes = serialized.encode("utf-8")
        compressed_state = zlib.compress(serialized_bytes, level=1)
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._owned_action(con,mid,lease)
            if seq != row["last_event_seq"] + 1:
                existing = con.execute(
                    "SELECT action_json,state_after_json,state_after_zlib,white_clock_ms,black_clock_ms,phase,side "
                    "FROM match_events WHERE match_id=? AND sequence_no=?",
                    (mid,seq),
                ).fetchone()
                if existing is None:
                    raise StoreError(409,"EVENT_SEQUENCE","Event sequence must advance by one.")
                stored_compressed = existing["state_after_zlib"]
                if stored_compressed is None:
                    try:
                        state_matches = json.loads(existing["state_after_json"]) == state
                    except (ValueError, TypeError, RecursionError):
                        state_matches = False
                else:
                    try:
                        decoder = zlib.decompressobj()
                        restored_state = decoder.decompress(stored_compressed, len(serialized_bytes) + 1)
                        state_matches = (
                            json.loads(restored_state) == state and decoder.eof
                            and not decoder.unused_data and not decoder.unconsumed_tail
                        )
                    except (ValueError, TypeError, RecursionError, zlib.error):
                        state_matches = False
                if (json.loads(existing["action_json"]) == action and state_matches
                        and existing["white_clock_ms"] == clocks["white"] and existing["black_clock_ms"] == clocks["black"]
                        and existing["phase"] == phase.upper() and existing["side"] == side.upper()):
                    return
                raise StoreError(409,"EVENT_CONFLICT","Event sequence already contains different data.")
            if clocks["white"] > row["white_clock_ms"] or clocks["black"] > row["black_clock_ms"]:
                raise StoreError(409,"CLOCK_CONFLICT","Action cannot increase a clock.")
            stamp = self._db_now(con)
            # Keep the legacy NOT NULL column empty; new snapshots use compact zlib bytes.
            con.execute("INSERT INTO match_events(match_id,sequence_no,phase,side,action_json,state_after_json,state_after_zlib,white_clock_ms,black_clock_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (mid,seq,phase.upper(),side.upper(),encoded,"",compressed_state,int(clocks["white"]),int(clocks["black"]),stamp))
            con.execute("UPDATE matches SET state_json=?,phase=?,side_to_act=?,move_count=?,white_clock_ms=?,black_clock_ms=?,clock_checkpoint_at=?,updated_at=?,last_event_seq=?,state_version=state_version+1 WHERE id=?",
                        (serialized,state["phase"].upper(),state["side"].upper(),state["move_count"],int(clocks["white"]),int(clocks["black"]),stamp,stamp,seq,mid))
        HUB.publish("match",mid,tid=row["tournament_id"],status="RUNNING",move_count=state["move_count"],phase=state["phase"])

    def save_state(self, mid: str, state: dict[str, Any], clocks: dict[str, int], *, lease: Lease) -> None:
        # State transitions must carry their action and sequence in one transaction.
        raise StoreError(409,"ACTION_REQUIRED","Use record_action to persist state atomically.")

    def finish_match(self, mid: str, result: str, reason: str, clocks: dict[str, int], *, lease: Lease) -> None:
        payload = json.dumps({"result":result,"reason":reason,"clocks":clocks},sort_keys=True,separators=(",",":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT * FROM matches WHERE id=?", (mid,)).fetchone()
            if previous and previous["status"] == "FINISHED" and previous["lease_epoch"] == lease.epoch and previous["result_hash"] == digest:
                attempt = con.execute("SELECT owner FROM match_attempts WHERE match_id=? AND epoch=?", (mid,lease.epoch)).fetchone()
                if attempt and attempt["owner"] == lease.owner:
                    return
            row = self._owned(con,mid,lease)
            if clocks["white"] > row["white_clock_ms"] or clocks["black"] > row["black_clock_ms"]:
                raise StoreError(409,"CLOCK_CONFLICT","Result cannot increase a clock.")
            stamp = self._db_now(con)
            con.execute("UPDATE matches SET status='FINISHED',result=?,result_reason=?,result_hash=?,side_to_act=NULL,white_clock_ms=?,black_clock_ms=?,finished_at=?,updated_at=? WHERE id=?",
                        (result,reason,digest,int(clocks["white"]),int(clocks["black"]),stamp,stamp,mid))
            self._release(con,mid,lease.epoch,"FINISHED")
            left = con.execute("SELECT COUNT(*) FROM matches WHERE tournament_id=? AND status<>'FINISHED'",(row["tournament_id"],)).fetchone()[0]
            if not left:
                con.execute("UPDATE tournaments SET status='FINISHED',finished_at=? WHERE id=? AND status='RUNNING'",(stamp,row["tournament_id"]))
        HUB.publish("match",mid,tid=row["tournament_id"],status="FINISHED")
        if not left:
            HUB.publish("tournament",row["tournament_id"],status="FINISHED")

    def pause_match(self, mid: str, reason: str, *, lease: Lease, retry: bool = False) -> None:
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            row = self._owned(con,mid,lease)
            eligible = retry and not row["execution_started"] and row["attempt_count"] < MAX_ATTEMPTS
            status = "SCHEDULED" if eligible else "PAUSED"
            con.execute("UPDATE matches SET status=?,result_reason=?,available_at=?,timing_uncertainty_ms=NULL,updated_at=? WHERE id=?",
                        (status,reason,self._db_now(con)+5,self._db_now(con),mid))
            self._release(con,mid,lease.epoch,reason)
        HUB.publish("match",mid,tid=row["tournament_id"],status=status)

    def resume_paused(self, tid: str) -> int:
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            blocked = con.execute("SELECT 1 FROM matches WHERE tournament_id=? AND status='PAUSED' AND (execution_started=1 OR attempt_count>=?) LIMIT 1",(tid,MAX_ATTEMPTS)).fetchone()
            if blocked:
                raise StoreError(409,"RECOVERY_REVIEW","Automatic resume cannot restore persistent bot memory or exceed retry limits.")
            count = con.execute("UPDATE matches SET status='SCHEDULED',result_reason=NULL,available_at=0 WHERE tournament_id=? AND status='PAUSED'",(tid,)).rowcount
        if count:
            HUB.publish("tournament",tid)
        return count

    def recover_running(self) -> int:
        updates = []
        with self.tx() as con:
            stamp = self._db_now(con)
            expired = con.execute(
                "SELECT 1 FROM matches WHERE status='RUNNING' "
                "AND COALESCE(lease_expires_at,0)<=? LIMIT 1",
                (stamp,),
            ).fetchone()
            if expired is None:
                return 0
            con.execute("BEGIN IMMEDIATE")
            stamp = self._db_now(con)
            rows = con.execute("SELECT * FROM matches WHERE status='RUNNING' AND COALESCE(lease_expires_at,0)<=?",(stamp,)).fetchall()
            for row in rows:
                retry = not row["execution_started"] and row["attempt_count"] < MAX_ATTEMPTS
                status = "SCHEDULED" if retry else "PAUSED"
                reason = "INFRA: LEASE_EXPIRED" if retry else "REVIEW: BOT_MEMORY_UNAVAILABLE" if row["execution_started"] else "REVIEW: RETRY_LIMIT"
                con.execute("UPDATE matches SET status=?,result_reason=?,available_at=?,timing_uncertainty_ms=NULL,updated_at=? WHERE id=?",
                            (status,reason,stamp+5,stamp,row["id"]))
                self._release(con,row["id"],row["lease_epoch"],reason,stamp=stamp)
                updates.append((row["id"],row["tournament_id"],status))
        for mid,tid,status in updates:
            HUB.publish("match",mid,tid=tid,status=status)
        return len(rows)

    def worker_metrics(self) -> dict[str, Any]:
        with self.tx() as con:
            stamp = self._db_now(con)
            counts = {r["status"]:r["total"] for r in con.execute("SELECT status,COUNT(*) AS total FROM matches GROUP BY status")}
            attempts = con.execute("SELECT COUNT(*) FROM match_attempts").fetchone()[0]
            oldest = con.execute("SELECT MIN(updated_at) FROM matches WHERE status='SCHEDULED'").fetchone()[0]
            retries = con.execute("SELECT COALESCE(SUM(MAX(0,attempt_count-1)),0) FROM matches").fetchone()[0]
            expired = con.execute("SELECT COUNT(*) FROM matches WHERE status='RUNNING' AND COALESCE(lease_expires_at,0)<=?",(stamp,)).fetchone()[0]
            compile_active = con.execute("SELECT COUNT(*) FROM resource_leases WHERE resource='compile' AND expires_at>?",(stamp,)).fetchone()[0]
            checkpoint = con.execute("SELECT MIN(COALESCE(clock_checkpoint_at,started_at)) FROM matches WHERE status='RUNNING'").fetchone()[0]
            receipts = {r["status"]:r["total"] for r in con.execute("SELECT status,COUNT(*) AS total FROM submission_receipts GROUP BY status")}
            receipt_oldest = con.execute("SELECT MIN(received_at) FROM submission_receipts WHERE status='QUEUED'").fetchone()[0]
            return {"matches":counts,"attempts_total":attempts,"retries_total":retries,"expired_leases":expired,
                    "submissions":receipts,"submission_oldest_age_seconds":max(0,stamp-receipt_oldest) if receipt_oldest is not None else 0,
                    "compile_active":compile_active,"queue_oldest_age_seconds":max(0,stamp-oldest) if oldest is not None else 0,
                    "checkpoint_oldest_age_seconds":max(0,stamp-checkpoint) if checkpoint is not None else 0}

    def acquire_compile_slot(self, owner: str, limit: int = 1, lease_seconds: int = 180) -> str | None:
        if not owner or limit < 1 or lease_seconds < 90:
            raise ValueError("Compile slot requires an owner, positive limit and at least 90 seconds.")
        with self.tx() as con:
            con.execute("BEGIN IMMEDIATE")
            stamp = self._db_now(con)
            con.execute("DELETE FROM resource_leases WHERE expires_at<=?",(stamp,))
            if con.execute("SELECT COUNT(*) FROM resource_leases WHERE resource='compile'").fetchone()[0] >= limit:
                return None
            token = secrets.token_urlsafe(24)
            con.execute("INSERT INTO resource_leases(token,owner,resource,expires_at) VALUES(?,?,'compile',?)",(token,owner,stamp+lease_seconds))
            return token

    def assert_compile_slot(self, owner: str, token: str) -> None:
        with self.tx() as con:
            self._assert_compile_slot(con,owner,token)

    def _assert_compile_slot(self, con: sqlite3.Connection, owner: str, token: str) -> None:
        with self._compile_flags_lock:
            failed = self._compile_flags.get(token)
        if failed is not None and failed.is_set():
            raise LeaseLost("Compiler lease renewal failed.")
        row = con.execute("SELECT 1 FROM resource_leases WHERE token=? AND owner=? AND resource='compile' AND expires_at>?",
                          (token,owner,Store._db_now(con))).fetchone()
        if not row:
            raise LeaseLost("Compile slot expired or was replaced.")

    @contextmanager
    def compile_slot(self, owner: str, limit: int = 1, lease_seconds: int = 180) -> Iterator[str]:
        token = self.acquire_compile_slot(owner,limit,lease_seconds)
        if token is None:
            raise LeaseLost("Compiler capacity is busy.")
        stopped, failed = threading.Event(), threading.Event()
        with self._compile_flags_lock:
            self._compile_flags[token] = failed
        def renew() -> None:
            self.enable_thread_connection_reuse()
            try:
                while not stopped.wait(5):
                    try:
                        with self.tx() as con:
                            con.execute("BEGIN IMMEDIATE")
                            self._assert_compile_slot(con,owner,token)
                            con.execute("UPDATE resource_leases SET expires_at=? WHERE token=? AND owner=?",
                                        (self._db_now(con)+lease_seconds,token,owner))
                    except Exception:
                        failed.set()
                        return
            finally:
                self.close_thread_connection()
        heartbeat = threading.Thread(target=renew,daemon=True,name="compile-lease")
        heartbeat.start()
        try:
            yield token
        finally:
            stopped.set()
            while heartbeat.is_alive():
                heartbeat.join(timeout=1)
            try:
                self.release_compile_slot(owner,token)
            except sqlite3.Error:
                LOG.exception("Could not release compiler lease; expiry will reclaim capacity.")
            finally:
                with self._compile_flags_lock:
                    self._compile_flags.pop(token,None)

    def release_compile_slot(self, owner: str, token: str) -> None:
        with self.tx() as con:
            con.execute("DELETE FROM resource_leases WHERE token=? AND owner=? AND resource='compile'",(token,owner))

    def entry_bot_paths_info(self, entry_id: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute(
                "SELECT e.id AS entry_id, e.bot_submission_id AS bot_id, u.username FROM entries e JOIN users u ON u.id=e.user_id WHERE e.id = ?",
                (entry_id,),
            ).fetchone()
        return dict(row) if row else None

    def user_count(self) -> int:
        with self.tx() as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def site_stats(self) -> dict[str, int]:
        """Read the public counters in one statement without tournament details."""
        with self.tx() as con:
            row = con.execute(
                "SELECT (SELECT COUNT(*) FROM users) AS users, "
                "(SELECT COUNT(*) FROM tournaments) AS tournaments, "
                "(SELECT COUNT(*) FROM tournaments WHERE status='RUNNING') AS running_tournaments, "
                "(SELECT COUNT(*) FROM matches m JOIN tournaments mt ON mt.id=m.tournament_id "
                "WHERE m.status IN ('RUNNING','PAUSED')) AS running_matches"
            ).fetchone()
        return dict(row)
