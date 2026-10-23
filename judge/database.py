"""SQLite persistence for users, sessions, and submissions."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from core.languages import language_profile


SESSION_TTL_SECONDS = 12 * 60 * 60
SCRYPT_N = 1 << 14


def _now() -> int:
    return int(time.time())


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=8, p=1, dklen=32)
    return "scrypt$" + str(SCRYPT_N) + "$" + salt.hex() + "$" + digest.hex()


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, cost, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt" or int(cost) != SCRYPT_N:
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=SCRYPT_N,
            r=8,
            p=1,
            dklen=32,
        )
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connections = threading.local()
        self._initialize()

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
    def _connect(self) -> Iterator[sqlite3.Connection]:
        reuse = getattr(self._connections, "enabled", False)
        connection = getattr(self._connections, "connection", None) if reuse else None
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            if reuse:
                self._connections.connection = connection
        try:
            with connection:
                yield connection
        finally:
            if not reuse:
                connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS submissions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    problem_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verdict TEXT,
                    score INTEGER NOT NULL DEFAULT 0,
                    test_results_json TEXT NOT NULL DEFAULT '[]',
                    message TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS submissions_user_created_idx
                    ON submissions(user_id);
                CREATE INDEX IF NOT EXISTS submissions_queue_idx
                    ON submissions(status);
                """
            )
            for index_name, column in (
                ("submissions_user_created_idx", "user_id"),
                ("submissions_queue_idx", "status"),
            ):
                columns = tuple(row["name"] for row in connection.execute(f"PRAGMA index_info({index_name})"))
                if columns != (column,):
                    connection.execute(f"DROP INDEX IF EXISTS {index_name}")
                    connection.execute(f"CREATE INDEX {index_name} ON submissions({column})")
            connection.execute(
                "UPDATE submissions SET status = 'QUEUED', updated_at = ? WHERE status = 'RUNNING'",
                (_now(),),
            )
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))

    @staticmethod
    def normalize_username(username: str) -> str:
        return username.lower()

    def register(self, username: str, password: str) -> dict[str, Any]:
        username = self.normalize_username(username)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, _hash_password(password), _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("USERNAME_TAKEN") from exc
            return {"user_id": int(cursor.lastrowid), "username": username}

    def authenticate(self, username: str, password: str) -> tuple[int, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, password_hash FROM users WHERE username = ?",
                (self.normalize_username(username),),
            ).fetchone()
        if row is None or not _verify_password(password, row["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (token_hash, row["id"], _now() + SESSION_TTL_SECONDS),
            )
        return int(row["id"]), token

    def user_for_token(self, token: str) -> int | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM sessions WHERE token_hash = ? AND expires_at > ?",
                (token_hash, _now()),
            ).fetchone()
        return int(row["user_id"]) if row else None

    def create_submission(self, user_id: int, problem_id: str, source: str, language: str = "cpp17") -> str:
        language_profile(language)
        submission_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO submissions(
                    id, user_id, problem_id, language, source, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?)
                """,
                (submission_id, user_id, problem_id, language, source, now, now),
            )
        return submission_id

    def claim_next_submission(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            select_next = (
                "SELECT id, user_id, problem_id, language, source FROM submissions "
                "WHERE status='QUEUED' ORDER BY rowid LIMIT 1"
            )
            row = connection.execute(select_next).fetchone()
            if row is None:
                return None
            connection.execute("BEGIN IMMEDIATE")
            while row is not None:
                changed = connection.execute(
                    "UPDATE submissions SET status='RUNNING',updated_at=? WHERE id=? AND status='QUEUED'",
                    (_now(), row["id"]),
                ).rowcount
                if changed == 1:
                    connection.commit()
                    return dict(row)
                row = connection.execute(select_next).fetchone()
            connection.commit()
            return None

    def finish_submission(
        self,
        submission_id: str,
        *,
        verdict: str,
        score: int,
        test_results: list[dict[str, Any]],
        message: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE submissions
                SET status = 'FINISHED', verdict = ?, score = ?, test_results_json = ?,
                    message = ?, updated_at = ?
                WHERE id = ?
                """,
                (verdict, score, json.dumps(test_results), message, _now(), submission_id),
            )

    def fail_submission(self, submission_id: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE submissions
                SET status = 'SYSTEM_ERROR', verdict = 'SYSTEM_ERROR', message = ?, updated_at = ?
                WHERE id = ?
                """,
                (message[:1000], _now(), submission_id),
            )

    @staticmethod
    def _submission_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "submission_id": row["id"],
            "problem_id": row["problem_id"],
            "language": row["language"],
            "status": row["status"],
            "verdict": row["verdict"],
            "score": row["score"],
            "test_results": json.loads(row["test_results_json"]),
            "message": row["message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_submission(self, user_id: int, submission_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM submissions WHERE id = ? AND user_id = ?",
                (submission_id, user_id),
            ).fetchone()
        return self._submission_dict(row) if row else None

    def list_submissions(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM submissions WHERE user_id = ? ORDER BY rowid DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [self._submission_dict(row) for row in rows]

