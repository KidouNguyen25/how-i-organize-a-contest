"""Maintenance and release evidence without changing host configuration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_runtime_lock = threading.Lock()
_runtime_cache: tuple[float, tuple[Any, ...], bool] | None = None


def _runtime_ready(sandbox: Any, info: dict[str, Any]) -> bool:
    global _runtime_cache
    identity = (id(sandbox), info.get("compiler_image_id"), info.get("runtime_image_id"))
    with _runtime_lock:
        if _runtime_cache and _runtime_cache[1] == identity and time.monotonic() - _runtime_cache[0] < 10:
            return _runtime_cache[2]
        if info["kind"] == "docker":
            image_ids = set(identity[1:])
            if None in image_ids:
                ok = False
            else:
                try:
                    probe = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", *sorted(image_ids)],
                                           capture_output=True, text=True, timeout=3)
                    ok = probe.returncode == 0 and set(probe.stdout.splitlines()) == image_ids
                except (OSError, subprocess.SubprocessError):
                    ok = False
        else:
            ok = sandbox.available()[0]
        _runtime_cache = (time.monotonic(), identity, ok)
        return ok


def maintenance_enabled(data_dir: Path) -> bool:
    return (data_dir / ".maintenance").exists()


def set_maintenance(data_dir: Path, enabled: bool) -> None:
    marker = data_dir / ".maintenance"
    if enabled:
        data_dir.mkdir(parents=True, exist_ok=True)
        with marker.open("a", encoding="utf-8") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    else:
        marker.unlink(missing_ok=True)


def readiness(store: Any, data_dir: Path, sandbox: Any) -> dict[str, Any]:
    """Report present checks, not an assertion of sandbox security or an SLA."""
    checks: dict[str, bool] = {}
    try:
        with store.tx() as con:
            checks["database"] = con.execute("SELECT 1").fetchone()[0] == 1
            schema = con.execute("PRAGMA user_version").fetchone()[0]
    except (sqlite3.Error, OSError):
        checks["database"] = False
        schema = None
    try:
        usage = shutil.disk_usage(data_dir)
        checks["disk"] = usage.free >= max(512 * 1024 * 1024, usage.total // 10)
    except OSError:
        checks["disk"] = False
    info = sandbox.policy_info()
    from ott.store import SCHEMA_VERSION
    checks["schema"] = schema == SCHEMA_VERSION
    checks["runtime"] = _runtime_ready(sandbox, info)
    checks["maintenance_clear"] = not maintenance_enabled(data_dir)
    return {"ready": all(checks.values()), "checks": checks, "schema_version": schema,
            "sandbox": info, "server_time": int(time.time())}


def release_manifest(data_dir: Path, sandbox: Any, *, workers: int = 1,
                     public_origin: str | None = None, profile: str = "demo",
                     move_delay_ms: int = 2000) -> dict[str, Any]:
    root = Path(__file__).resolve().parent.parent
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, timeout=5, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                                    text=True, timeout=5, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    from ott.store import SCHEMA_VERSION
    from ott.adapters import get_adapter

    hashes = getattr(sandbox, "allowed_source_hashes", None)
    allowlist_hash = hashlib.sha256(json.dumps(sorted(hashes), separators=(",", ":")).encode()).hexdigest() if hashes is not None else None
    config = {"workers": workers, "compile_slots": 1, "public_origin": public_origin,
              "profile": profile, "move_delay_ms": move_delay_ms,
              "source_allowlist_sha256": allowlist_hash,
              "sandbox": sandbox.policy_info(), "schema_version": SCHEMA_VERSION,
              "adapter": {"id": get_adapter().adapter_id, "version": get_adapter().version}}
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"manifest_version": 1, "kind": "planned-runtime-config", "created_at": int(time.time()), "git_commit": commit,
            "working_tree_dirty": dirty, "config": config, "config_sha256": hashlib.sha256(encoded).hexdigest(),
            "restore_requires_empty_destination": True,
            "rollback_requires_compatible_schema": True}


def write_manifest(destination: Path, manifest: dict[str, Any]) -> None:
    if destination.exists():
        raise ValueError("Release manifest destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".release-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Linking publishes without replacing another release manifest.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
