"""Offline, verified SQLite and artifact backup packages.

All serving and worker processes must hold a shared DataDirectoryLock. Package
creation additionally requires an explicit declaration that older processes,
which do not implement that lock, have stopped. This is not an online backup
protocol for mutable compiler output.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

FORMAT_VERSION = 1
REQUIRED_TABLES = {
    "users", "sessions", "email_verification_tokens", "tournaments", "bot_submissions", "entries", "matches",
    "match_events", "audit_log",
}


class BackupError(RuntimeError):
    """The operation cannot preserve or verify the data safely."""


def _checked_path(path: str | Path) -> Path:
    candidate = Path(os.path.abspath(path))
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise BackupError(f"Symlink or junction is not allowed: {part}")
        if part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
            raise BackupError(f"Reparse point is not allowed: {part}")
    return candidate


class DataDirectoryLock:
    """Coordinate runtime readers and exclusive maintenance on one host.

    Unix supports multiple shared runtime holders. Windows uses an exclusive
    byte lock even for shared=True, so a second runtime fails instead of losing
    protection. Separate API and worker processes therefore require Unix.
    """

    def __init__(self, data_dir: str | Path, *, shared: bool = False, lock_name: str = ".runtime-lock") -> None:
        if lock_name not in (".runtime-lock", ".api-lock"):
            raise ValueError("Unsupported runtime lock name.")
        self.data_dir = _checked_path(data_dir)
        self.lock_name = lock_name
        self.shared = shared
        self.mode = "exclusive-windows" if os.name == "nt" else ("shared" if shared else "exclusive")
        self._file: Any = None

    def __enter__(self) -> "DataDirectoryLock":
        if not self.data_dir.is_dir():
            raise BackupError(f"Data directory does not exist: {self.data_dir}")
        lock_path = _checked_path(self.data_dir / self.lock_name)
        self._file = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0, os.SEEK_END)
                if not self._file.tell():
                    self._file.write(b"0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                mode = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
                fcntl.flock(self._file.fileno(), mode | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            detail = " Windows runtime locks are exclusive; use a Linux VM for separate API and worker processes." if os.name == "nt" else ""
            raise BackupError(f"Data directory is in use; stop conflicting processes.{detail}") from exc
        return self

    def __exit__(self, *args: Any) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _value(value: Any) -> Any:
    return {"sqlite_blob_hex": value.hex()} if isinstance(value, bytes) else value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def _read_connection(path: Path, *, immutable: bool = True) -> Iterator[sqlite3.Connection]:
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
    finally:
        con.close()


def _table_digest(con: sqlite3.Connection, table: str, columns: list[str], keys: list[str]) -> dict[str, Any]:
    selected = ",".join(_identifier(column) for column in columns)
    ordered = ",".join(_identifier(column) for column in (keys or columns))
    digest, ids, count = hashlib.sha256(), hashlib.sha256(), 0
    for row in con.execute(f"SELECT {selected} FROM {_identifier(table)} ORDER BY {ordered}"):
        digest.update(_json([_value(row[column]) for column in columns]) + b"\n")
        ids.update(_json([_value(row[column]) for column in (keys or columns)]) + b"\n")
        count += 1
    return {"columns": columns, "primary_key": keys, "count": count, "rows_sha256": digest.hexdigest(), "ids_sha256": ids.hexdigest()}


def inspect_database(path: str | Path) -> dict[str, Any]:
    """Inspect a closed snapshot without application startup or data changes."""
    db_path = _checked_path(path)
    if not db_path.is_file():
        raise BackupError(f"Database does not exist: {db_path}")
    # Immutable mode deliberately ignores WAL. Only closed package snapshots
    # are accepted, so outstanding WAL would otherwise hide committed data.
    if any(Path(str(db_path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise BackupError("Close and checkpoint the snapshot before inspecting it.")
    with _read_connection(db_path) as con:
        integrity = [row[0] for row in con.execute("PRAGMA integrity_check")]
        if integrity != ["ok"] or con.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("Database integrity or foreign key validation failed.")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        from ott.store import SCHEMA_VERSION
        if not 1 <= version <= SCHEMA_VERSION:
            raise BackupError(f"Unsupported database schema version: {version}")
        tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if not REQUIRED_TABLES.issubset(tables):
            raise BackupError("Database is missing required contest tables.")
        summaries = {}
        for table in tables:
            info = list(con.execute(f"PRAGMA table_info({_identifier(table)})"))
            columns = [row["name"] for row in info]
            keys = [row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"]]
            summaries[table] = _table_digest(con, table, columns, keys)
        for row in con.execute("SELECT id,source,source_sha256,source_size,source_purged_at FROM bot_submissions"):
            if row["source"] is None:
                if row["source_purged_at"] is None:
                    raise BackupError(f"Submission {row['id']} has missing source without a purge marker.")
            else:
                raw = row["source"].encode("utf-8")
                if len(raw) != row["source_size"] or hashlib.sha256(raw).hexdigest() != row["source_sha256"]:
                    raise BackupError(f"Source hash or byte count mismatch: {row['id']}")
        result_columns = [column for column in summaries["matches"]["columns"] if column in ("id", "status", "result", "result_reason", "engine_version", "state_version") or "revision" in column]
        if "submission_receipts" in tables:
            for receipt in con.execute("SELECT * FROM submission_receipts"):
                raw = receipt["source"].encode("utf-8")
                if not raw or len(raw) > 256 * 1024 or b"\x00" in raw or hashlib.sha256(raw).hexdigest() != receipt["source_hash"]:
                    raise BackupError("Accepted receipt source hash or format is invalid.")
                entry = con.execute("SELECT tournament_id,user_id FROM entries WHERE id=?",(receipt["entry_id"],)).fetchone()
                if not entry or entry["tournament_id"] != receipt["tournament_id"] or entry["user_id"] != receipt["user_id"]:
                    raise BackupError("Accepted receipt ownership does not match its entry.")
                if receipt["status"] == "READY":
                    bot = con.execute("SELECT source_sha256,tournament_id,user_id,language FROM bot_submissions WHERE id=?",(receipt["bot_id"],)).fetchone()
                    language = receipt["language"] if "language" in receipt.keys() else "cpp17"
                    if (not bot or bot["source_sha256"] != receipt["source_hash"] or bot["tournament_id"] != receipt["tournament_id"]
                            or bot["user_id"] != receipt["user_id"] or bot["language"] != language):
                        raise BackupError("Completed receipt differs from its submitted bot.")
        result_summary = _table_digest(con, "matches", result_columns, ["id"])
        schema = [tuple(row) for row in con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        return {"schema_version": version, "schema_sha256": hashlib.sha256(_json(schema)).hexdigest(), "tables": summaries, "results": result_summary}


def _files(root: Path) -> list[Path]:
    result = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = _checked_path(Path(parent) / name)
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise BackupError(f"Only regular files and directories are allowed: {path}")
            if stat.S_ISREG(mode) and path.stat().st_nlink > 1:
                raise BackupError(f"Hard-linked files are not allowed: {path}")
        result.extend(Path(parent) / name for name in files)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def _inventory(root: Path) -> list[dict[str, Any]]:
    result = []
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        if relative != "ott.db" and not relative.startswith("artifacts/"):
            raise BackupError(f"Unexpected package file: {relative}")
        kind = "database" if relative == "ott.db" else ("compile-cache" if relative.split("/")[1].startswith("check-") else "artifact")
        result.append({"path": relative, "size": path.stat().st_size, "sha256": _hash_file(path), "mode": stat.S_IMODE(path.stat().st_mode), "kind": kind})
    return result


def _destination(destination: str | Path, source: Path) -> Path:
    target = _checked_path(destination)
    if target.exists():
        raise BackupError("Destination must not exist; existing data is never overwritten.")
    if not target.parent.is_dir():
        raise BackupError("Destination parent directory must already exist.")
    if target == source or source in target.parents:
        raise BackupError("Destination must be outside the source directory.")
    return target


def _stage_for(target: Path) -> Path:
    return Path(tempfile.mkdtemp(prefix=f".{target.name}.pending-", dir=target.parent))


def _discard(stage: Path, parent: Path) -> None:
    checked = _checked_path(stage)
    if checked.parent != parent or ".pending-" not in checked.name:
        raise BackupError("Refusing to remove an unexpected staging path.")
    shutil.rmtree(checked)


def _sync_tree(stage: Path) -> None:
    for path in _files(stage):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    if os.name != "nt":
        directories = [Path(parent) for parent, _, _ in os.walk(stage)]
        for directory in reversed(directories):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def _publish(stage: Path, target: Path) -> None:
    _sync_tree(stage)
    if _checked_path(target).exists():
        raise BackupError("Destination appeared during the operation; refusing to replace it.")
    # Staging and destination share a parent and filesystem. Nonempty existing
    # directories cannot be replaced, preserving any concurrently created data.
    os.rename(stage, target)
    if os.name != "nt":
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_json(path: Path, value: Any) -> None:
    with path.open("xb") as stream:
        stream.write(_json(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def create_backup(data_dir: str | Path, destination: str | Path, *, offline: bool = False) -> dict[str, Any]:
    """Publish a new package after all API, compiler, worker and watcher stop."""
    if not offline:
        raise BackupError("Backup requires offline=True after stopping every API, compiler, worker and coding watcher, including older versions.")
    source = _checked_path(data_dir)
    target = _destination(destination, source)
    with DataDirectoryLock(source):
        db_path = _checked_path(source / "ott.db")
        if not db_path.is_file():
            raise BackupError("Data directory has no ott.db.")
        stage = _stage_for(target)
        try:
            # A write reservation also rejects unexpected SQLite writers while
            # the independent readonly connection performs the backup API copy.
            guard = sqlite3.connect(db_path, timeout=0)
            try:
                guard.execute("BEGIN IMMEDIATE")
                with _read_connection(db_path, immutable=False) as original:
                    copied = sqlite3.connect(stage / "ott.db")
                    try:
                        original.backup(copied)
                        copied.execute("PRAGMA journal_mode=DELETE")
                    finally:
                        copied.close()
                artifacts = _checked_path(source / "artifacts")
                if artifacts.exists():
                    _files(artifacts)
                    # Preserve any concurrently introduced symlink as a link,
                    # so inventory rejects it rather than copying its target.
                    shutil.copytree(artifacts, stage / "artifacts", symlinks=True)
                database = inspect_database(stage / "ott.db")
                manifest = {"format_version": FORMAT_VERSION, "created_at": int(time.time()), "consistency": "offline-exclusive-lock", "database": database, "files": _inventory(stage), "excluded": ["seed-accounts.json", ".runtime-lock", "logs", "external-secrets"], "source_inventory": "Committed submissions and drafts are in ott.db; artifacts includes final builds and check-* caches."}
                _write_json(stage / "manifest.json", manifest)
                verify_backup(stage)
            finally:
                guard.close()
            _publish(stage, target)
            return {"path": str(target), "manifest": manifest}
        finally:
            if stage.exists():
                _discard(stage, target.parent)


def verify_backup(package_dir: str | Path) -> dict[str, Any]:
    """Verify every file, schema, row/ID digest, result and source hash."""
    package = _checked_path(package_dir)
    if not package.is_dir():
        raise BackupError("Backup package directory does not exist.")
    manifest_path = _checked_path(package / "manifest.json")
    try:
        if manifest_path.stat().st_size > 32 * 1024 * 1024:
            raise BackupError("Backup manifest exceeds 32 MiB.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
            raise BackupError("Unsupported backup format.")
        listed = manifest.get("files")
        if not isinstance(listed, list):
            raise BackupError("Invalid backup file inventory.")
        seen = set()
        for entry in listed:
            relative = entry["path"]
            if not isinstance(relative, str) or not relative:
                raise BackupError("Unsafe path in backup manifest.")
            path = PurePosixPath(relative)
            if path.is_absolute() or "\\" in relative or ":" in relative or any(part in ("", ".", "..") for part in relative.split("/")):
                raise BackupError("Unsafe path in backup manifest.")
            if relative in seen:
                raise BackupError("Duplicate path in backup manifest.")
            seen.add(relative)
        if _inventory(package) != listed:
            raise BackupError("Backup file set, size or SHA-256 mismatch.")
        if inspect_database(package / "ott.db") != manifest.get("database"):
            raise BackupError("Backup database reconciliation failed.")
        return manifest
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        raise BackupError(f"Invalid backup package: {exc}") from exc


def _copy_payload(package: Path, stage: Path) -> None:
    shutil.copy2(package / "ott.db", stage / "ott.db", follow_symlinks=False)
    if (package / "artifacts").exists():
        shutil.copytree(package / "artifacts", stage / "artifacts", symlinks=True)


def restore_backup(package_dir: str | Path, destination: str | Path) -> dict[str, Any]:
    """Restore only to a new data directory; never replace a live directory."""
    package = _checked_path(package_dir)
    target = _destination(destination, package)
    manifest = verify_backup(package)
    stage = _stage_for(target)
    try:
        _copy_payload(package, stage)
        if _inventory(stage) != manifest["files"] or inspect_database(stage / "ott.db") != manifest["database"]:
            raise BackupError("Restored payload does not match the verified package.")
        _publish(stage, target)
        return {"path": str(target), "schema_version": manifest["database"]["schema_version"], "files": len(manifest["files"]), "overwritten": False}
    finally:
        if stage.exists():
            _discard(stage, target.parent)


def rehearse_migration(package_dir: str | Path, destination: str | Path) -> dict[str, Any]:
    """Migrate a private copy and reconcile all original business columns."""
    package = _checked_path(package_dir)
    target = _destination(destination, package)
    manifest = verify_backup(package)
    stage = _stage_for(target)
    try:
        _copy_payload(package, stage)
        if _inventory(stage) != manifest["files"]:
            raise BackupError("Migration rehearsal input does not match the package.")
        from ott.store import Store
        started = time.time()
        with DataDirectoryLock(stage):
            Store(stage / "ott.db")
            # No dispatcher is constructed: Store migrations are the only
            # application operation permitted against the rehearsal copy.
            migrated = inspect_database(stage / "ott.db")
            with _read_connection(stage / "ott.db") as after, _read_connection(package / "ott.db") as before:
                for table, summary in manifest["database"]["tables"].items():
                    if table == "sessions":
                        old = {row["token_hash"]: tuple(row) for row in before.execute("SELECT * FROM sessions")}
                        new = {row["token_hash"]: tuple(row) for row in after.execute("SELECT * FROM sessions")}
                        expiry = {row["token_hash"]: row["expires_at"] for row in before.execute("SELECT token_hash,expires_at FROM sessions")}
                        if any(key not in old or old[key] != value for key, value in new.items()) or any(expiry[key] > int(time.time()) for key in old.keys() - new.keys()):
                            raise BackupError("Migration changed sessions other than expiring old sessions.")
                        continue
                    observed = _table_digest(after, table, summary["columns"], summary["primary_key"])
                    if observed != summary:
                        raise BackupError(f"Migration changed original business data: {table}")
        (stage / ".runtime-lock").unlink()
        # Compare artifacts separately because the migrated DB necessarily has
        # a new byte hash and may contain new schema fields/tables.
        old_artifacts = [item for item in manifest["files"] if item["path"].startswith("artifacts/")]
        new_artifacts = [item for item in _inventory(stage) if item["path"].startswith("artifacts/")]
        if old_artifacts != new_artifacts:
            raise BackupError("Migration altered an artifact.")
        report = {"operation": "sqlite-migration-rehearsal", "source_package": str(package), "source_manifest_sha256": _hash_file(package / "manifest.json"), "from_schema": manifest["database"]["schema_version"], "to_schema": migrated["schema_version"], "duration_seconds": round(time.time() - started, 3), "original_columns_reconciled": True, "artifact_hashes_reconciled": True, "expired_sessions_may_be_removed": True, "database": migrated, "postgresql_migration": False}
        _write_json(stage / "migration-report.json", report)
        _publish(stage, target)
        return {"path": str(target), "report": report, "overwritten": False}
    finally:
        if stage.exists():
            _discard(stage, target.parent)
