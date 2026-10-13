"""SQLite persistence for the OTT tournament platform."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from judge.database import _hash_password, _verify_password
from ott.events import HUB

SESSION_TTL = 12 * 60 * 60
ENGINE_VERSION = "ott-1.0"
SCHEMA_VERSION = 1

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
        self._migrate()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 15000")
        try:
            with con:
                yield con
        finally:
            con.close()

    def _migrate(self) -> None:
        with self.tx() as con:
            con.execute("PRAGMA journal_mode = WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                con.executescript(SCHEMA)
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version > SCHEMA_VERSION:
                raise RuntimeError("Database is newer than this server.")
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
        encoded = row["password_hash"] if row else _hash_password("dummy-password-value")
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

    def get_tournament(self, tid: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute(self._TOUR_SQL + "WHERE t.id = ?", (tid,)).fetchone()
        return dict(row) if row else None

    def list_tournaments(self) -> list[dict[str, Any]]:
        with self.tx() as con:
            rows = con.execute(self._TOUR_SQL + "ORDER BY t.created_at DESC").fetchall()
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

    _ENTRY_SQL = (
        "SELECT e.id, e.tournament_id, e.user_id, e.status, e.registered_at, e.bot_submission_id, u.username, "
        "e.submitted_at, e.submit_note, e.draft_updated_at, e.compile_count, e.last_ok_sha, "
        "b.compile_status, b.created_at AS bot_created_at, b.source_size "
        "FROM entries e JOIN users u ON u.id = e.user_id LEFT JOIN bot_submissions b ON b.id = e.bot_submission_id "
    )

    def get_entry(self, eid: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute(self._ENTRY_SQL + "WHERE e.id = ?", (eid,)).fetchone()
        return dict(row) if row else None

    def get_user_entry(self, tid: str, user_id: int) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute(self._ENTRY_SQL + "WHERE e.tournament_id = ? AND e.user_id = ?", (tid, user_id)).fetchone()
        return dict(row) if row else None

    def list_entries(self, tid: str, include_removed: bool = False) -> list[dict[str, Any]]:
        extra = "" if include_removed else "AND e.status <> 'REMOVED' "
        with self.tx() as con:
            rows = con.execute(
                self._ENTRY_SQL + f"WHERE e.tournament_id = ? {extra}ORDER BY e.registered_at, e.id", (tid,)
            ).fetchall()
        return [dict(r) for r in rows]

    def user_entries(self, user_id: int) -> list[dict[str, Any]]:
        with self.tx() as con:
            rows = con.execute(self._ENTRY_SQL + "WHERE e.user_id = ? AND e.status <> 'REMOVED'", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def remove_entry(self, entry_id: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE entries SET status = 'REMOVED', bot_submission_id = NULL WHERE id = ?", (entry_id,))
            row = con.execute("SELECT tournament_id FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row:
            HUB.publish("entry", row["tournament_id"])

    def get_draft(self, entry_id: str) -> tuple[str | None, int | None]:
        with self.tx() as con:
            row = con.execute("SELECT draft_source, draft_updated_at FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return (row["draft_source"], row["draft_updated_at"]) if row else (None, None)

    def save_draft(self, entry_id: str, source: str) -> int:
        stamp = now()
        with self.tx() as con:
            cur = con.execute(
                "UPDATE entries SET draft_source = ?, draft_updated_at = ? WHERE id = ? AND submitted_at IS NULL",
                (source, stamp, entry_id),
            )
            if cur.rowcount != 1:
                raise StoreError(409, "ALREADY_SUBMITTED", "The bot was already submitted and cannot be changed.")
        return stamp

    def finalize_entry(self, entry_id: str, tid: str, user_id: int, bot_id: str, source: str) -> bool:
        """Atomically create the single, immutable submission. False if the entry was already submitted."""
        raw = source.encode("utf-8")
        stamp = now()
        with self.tx() as con:
            row = con.execute("SELECT submitted_at, status FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if not row or row["submitted_at"] is not None or row["status"] == "REMOVED":
                return False
            con.execute(
                "INSERT INTO bot_submissions(id,tournament_id,user_id,language,source,source_sha256,source_size,compile_status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (bot_id, tid, user_id, "cpp17", source, hashlib.sha256(raw).hexdigest(), len(raw), "READY", stamp),
            )
            con.execute(
                "UPDATE entries SET bot_submission_id = ?, status = 'READY', submitted_at = ?, draft_source = ?, draft_updated_at = ?, submit_note = NULL WHERE id = ?",
                (bot_id, stamp, source, stamp, entry_id),
            )
        HUB.publish("entry", tid)
        return True

    def bump_compile(self, entry_id: str, limit: int) -> int:
        """Atomically consume one compile attempt; raise 429 when the limit is exhausted."""
        with self.tx() as con:
            cur = con.execute(
                "UPDATE entries SET compile_count = compile_count + 1 WHERE id = ? AND compile_count < ? AND submitted_at IS NULL",
                (entry_id, limit),
            )
            if cur.rowcount != 1:
                raise StoreError(429, "COMPILE_LIMIT", f"You have used all {limit} compile attempts.")
            return con.execute("SELECT compile_count FROM entries WHERE id = ?", (entry_id,)).fetchone()[0]

    def set_last_ok(self, entry_id: str, sha: str | None) -> None:
        with self.tx() as con:
            con.execute("UPDATE entries SET last_ok_sha = ? WHERE id = ?", (sha, entry_id))

    def set_submit_note(self, entry_id: str, note: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE entries SET submit_note = ? WHERE id = ? AND submitted_at IS NULL", (note, entry_id))
            row = con.execute("SELECT tournament_id FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row:
            HUB.publish("entry", row["tournament_id"])

    def expired_coding(self) -> list[dict[str, Any]]:
        with self.tx() as con:
            rows = con.execute(
                "SELECT t.id AS tid, e.id AS eid, e.user_id, e.draft_source FROM tournaments t JOIN entries e ON e.tournament_id = t.id "
                "WHERE t.status = 'CODING' AND t.coding_deadline <= ? AND e.submitted_at IS NULL AND e.status <> 'REMOVED' AND e.submit_note IS NULL",
                (now(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_bot(self, bid: str, with_source: bool = False) -> dict[str, Any] | None:
        cols = "*" if with_source else "id,tournament_id,user_id,language,source_sha256,source_size,compile_status,compile_message,created_at"
        with self.tx() as con:
            row = con.execute(f"SELECT {cols} FROM bot_submissions WHERE id = ?", (bid,)).fetchone()
        return dict(row) if row else None

    # ---------------- matches ----------------
    def create_matches(self, tid: str, rows: list[dict[str, Any]], clock_ms: int) -> None:
        with self.tx() as con:
            if con.execute("SELECT 1 FROM matches WHERE tournament_id = ? LIMIT 1", (tid,)).fetchone():
                raise StoreError(409, "SCHEDULE_EXISTS", "Schedule already exists.")
            for r in rows:
                con.execute(
                    "INSERT INTO matches(id,tournament_id,round_no,leg_no,pairing_no,white_entry_id,black_entry_id,status,phase,"
                    "white_clock_ms,black_clock_ms,updated_at,engine_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), tid, r["round_no"], r["leg_no"], r["pairing_no"], r["white"], r["black"],
                     "SCHEDULED", "PLACEMENT", clock_ms, clock_ms, now(), ENGINE_VERSION),
                )
        HUB.publish("tournament", tid)

    _MATCH_SQL = (
        "SELECT m.*, t.name AS tournament_name, t.clock_seconds, wu.username AS white_name, bu.username AS black_name "
        "FROM matches m JOIN tournaments t ON t.id = m.tournament_id "
        "JOIN entries we ON we.id = m.white_entry_id JOIN users wu ON wu.id = we.user_id "
        "JOIN entries be ON be.id = m.black_entry_id JOIN users bu ON bu.id = be.user_id "
    )

    def get_match(self, mid: str) -> dict[str, Any] | None:
        with self.tx() as con:
            row = con.execute(self._MATCH_SQL + "WHERE m.id = ?", (mid,)).fetchone()
        return dict(row) if row else None

    def list_matches(self, tid: str | None = None, statuses: tuple[str, ...] | None = None, limit: int = 500) -> list[dict[str, Any]]:
        where, args = [], []
        if tid:
            where.append("m.tournament_id = ?")
            args.append(tid)
        if statuses:
            where.append(f"m.status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        sql = self._MATCH_SQL + (f"WHERE {' AND '.join(where)} " if where else "")
        sql += "ORDER BY m.leg_no, m.round_no, m.pairing_no LIMIT ?"
        with self.tx() as con:
            rows = con.execute(sql, (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def user_matches(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        with self.tx() as con:
            rows = con.execute(
                self._MATCH_SQL + "WHERE we.user_id = ? OR be.user_id = ? ORDER BY m.updated_at DESC LIMIT ?",
                (user_id, user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_match(self, mid: str) -> bool:
        with self.tx() as con:
            cur = con.execute(
                "UPDATE matches SET status='RUNNING', started_at=COALESCE(started_at, ?), updated_at=? "
                "WHERE id = ? AND status IN ('SCHEDULED','PAUSED','RUNNING')",
                (now(), now(), mid),
            )
            claimed = cur.rowcount == 1
            row = con.execute("SELECT tournament_id FROM matches WHERE id = ?", (mid,)).fetchone()
        if claimed and row:
            HUB.publish("match", mid, tid=row["tournament_id"], status="RUNNING")
        return claimed

    def checkpoint(self, mid: str, white_ms: int, black_ms: int) -> None:
        with self.tx() as con:
            con.execute(
                "UPDATE matches SET white_clock_ms=?, black_clock_ms=?, clock_checkpoint_at=?, updated_at=? WHERE id=?",
                (int(white_ms), int(black_ms), now(), now(), mid),
            )

    def record_action(self, mid: str, seq: int, state: dict[str, Any], action: dict[str, Any], side: str, phase: str, clocks: dict[str, int]) -> None:
        light = {"board": state["board"], "move_count": state["move_count"], "phase": state["phase"], "side": state["side"]}
        with self.tx() as con:
            con.execute(
                "INSERT OR IGNORE INTO match_events(match_id,sequence_no,phase,side,action_json,state_after_json,white_clock_ms,black_clock_ms,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, seq, phase.upper(), side.upper(), json.dumps(action, separators=(",", ":")), json.dumps(light, separators=(",", ":")),
                 int(clocks["white"]), int(clocks["black"]), now()),
            )
            con.execute(
                "UPDATE matches SET state_json=?, phase=?, side_to_act=?, move_count=?, white_clock_ms=?, black_clock_ms=?, "
                "clock_checkpoint_at=?, updated_at=? WHERE id=?",
                (json.dumps(state, separators=(",", ":")), state["phase"].upper(), state["side"].upper(), state["move_count"],
                 int(clocks["white"]), int(clocks["black"]), now(), now(), mid),
            )
            row = con.execute("SELECT tournament_id FROM matches WHERE id = ?", (mid,)).fetchone()
        HUB.publish("match", mid, tid=row["tournament_id"], status="RUNNING", move_count=state["move_count"], phase=state["phase"])

    def save_state(self, mid: str, state: dict[str, Any], clocks: dict[str, int]) -> None:
        with self.tx() as con:
            con.execute(
                "UPDATE matches SET state_json=?, phase=?, side_to_act=?, white_clock_ms=?, black_clock_ms=?, updated_at=? WHERE id=?",
                (json.dumps(state, separators=(",", ":")), state["phase"].upper(), state["side"].upper(),
                 int(clocks["white"]), int(clocks["black"]), now(), mid),
            )

    def finish_match(self, mid: str, result: str, reason: str, clocks: dict[str, int]) -> None:
        with self.tx() as con:
            con.execute(
                "UPDATE matches SET status='FINISHED', result=?, result_reason=?, side_to_act=NULL, white_clock_ms=?, black_clock_ms=?, "
                "finished_at=?, updated_at=? WHERE id=?",
                (result, reason, int(clocks["white"]), int(clocks["black"]), now(), now(), mid),
            )
            row = con.execute("SELECT tournament_id FROM matches WHERE id = ?", (mid,)).fetchone()
            left = con.execute(
                "SELECT COUNT(*) FROM matches WHERE tournament_id = ? AND status <> 'FINISHED'", (row["tournament_id"],)
            ).fetchone()[0]
            if left == 0:
                con.execute(
                    "UPDATE tournaments SET status='FINISHED', finished_at=? WHERE id=? AND status='RUNNING'",
                    (now(), row["tournament_id"]),
                )
        HUB.publish("match", mid, tid=row["tournament_id"], status="FINISHED")
        if left == 0:
            HUB.publish("tournament", row["tournament_id"], status="FINISHED")

    def pause_match(self, mid: str, reason: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE matches SET status='PAUSED', result_reason=?, updated_at=? WHERE id=?", (reason, now(), mid))
            row = con.execute("SELECT tournament_id FROM matches WHERE id = ?", (mid,)).fetchone()
        if row:
            HUB.publish("match", mid, tid=row["tournament_id"], status="PAUSED")

    def resume_paused(self, tid: str) -> int:
        with self.tx() as con:
            cur = con.execute(
                "UPDATE matches SET status='SCHEDULED', result_reason=NULL, updated_at=? WHERE tournament_id=? AND status='PAUSED'",
                (now(), tid),
            )
            count = cur.rowcount
        if count:
            HUB.publish("tournament", tid)
        return count

    def recover_running(self) -> None:
        with self.tx() as con:
            con.execute("UPDATE matches SET status='SCHEDULED' WHERE status='RUNNING'")

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
