"""Competition service, worker and offline data operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from ott import runner
from ott.api import make_app, serve
from ott.backup import (BackupError, DataDirectoryLock, create_backup, rehearse_migration,
                        restore_backup, verify_backup)
from ott.operations import release_manifest, set_maintenance, write_manifest
from ott.sandbox import IMAGE, InfraError, make_sandbox
from ott.security import ServerPolicy
from ott.store import Store
from ott.tournament import Dispatcher

ROOT = Path(__file__).resolve().parent.parent
SEED_USERS = [("organizer", "ORGANIZER"), ("team_alpha", "CONTESTANT"), ("team_beta", "CONTESTANT"),
              ("team_gamma", "CONTESTANT"), ("team_delta", "CONTESTANT")]


def seed(data_dir: Path) -> None:
    store = Store(data_dir / "ott.db")
    creds_path = data_dir / "seed-accounts.json"
    if creds_path.is_symlink():
        raise ValueError("Credentials file must not be a symlink.")
    creds = json.loads(creds_path.read_text(encoding="utf-8")) if creds_path.exists() else {}
    for username, role in SEED_USERS:
        if store.get_user(username):
            store.set_role(username, role)
            continue
        password = secrets.token_urlsafe(12)
        store.create_user(username, password, role)
        creds[username] = {"role": role, "password": password}
        print(f"created {role.lower()} account: {username}")
    fd = os.open(creds_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(creds, stream, indent=2, ensure_ascii=False)
    os.chmod(creds_path, 0o600)
    print(f"credentials file: {creds_path}")


def _hashes(path: Path | None) -> list[str] | None:
    if path is None:
        return None
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Source allowlist is too large.")
    value = json.loads(path.read_text(encoding="utf-8"))
    hashes = value.get("source_sha256") if isinstance(value, dict) else value
    if not isinstance(hashes, list) or not hashes or any(
            not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in hashes):
        raise ValueError("Source allowlist must contain a nonempty source_sha256 array of SHA-256 hashes.")
    return hashes


def _sandbox(args):
    if args.profile == "demo" and args.runner == "local":
        raise ValueError("Demo requires Docker. Local runner is development only.")
    hashes = _hashes(args.source_allowlist)
    if args.profile == "demo" and hashes is None:
        raise ValueError("Demo requires --source-allowlist containing organizer-approved source hashes.")
    sandbox = make_sandbox(args.runner, args.image, runtime_image=args.runtime_image,
                           allowed_source_hashes=hashes)
    ok, detail = sandbox.available()
    if not ok:
        raise InfraError(f"Runner unavailable: {detail}")
    runner.set_sandbox(sandbox)
    return sandbox


def _runtime_options(parser):
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--profile", choices=("demo", "development"), default="demo")
    parser.add_argument("--runner", choices=("auto", "docker", "local"), default="docker")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--runtime-image")
    parser.add_argument("--source-allowlist", type=Path)
    parser.add_argument("--move-delay-ms", type=int, default=2000)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main() -> None:
    parser = argparse.ArgumentParser(prog="ott")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="run private API and optional match dispatcher")
    _runtime_options(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--web", type=Path, default=ROOT / "mockup")
    p_serve.add_argument("--no-register", action="store_true")
    p_serve.add_argument("--secure-cookie", action="store_true")
    p_serve.add_argument("--no-dispatch", action="store_true", help="run a separate worker for matches; compilation remains in this API")
    p_serve.add_argument("--disable-stream", action="store_true")
    p_serve.add_argument("--public-origin", default=os.environ.get("CONTEST_PUBLIC_ORIGIN"))
    p_serve.add_argument("--connections", type=int, default=64)
    p_worker = sub.add_parser("worker", help="dispatch matches independently on the same Linux VM")
    _runtime_options(p_worker)
    p_build = sub.add_parser("build-image")
    p_build.add_argument("--image", default=IMAGE)
    p_build.add_argument("--target", choices=("compiler", "runtime"), default="compiler")
    p_build.add_argument("--base-image", help="approved base, preferably name@sha256:digest")
    p_seed = sub.add_parser("seed")
    p_seed.add_argument("--data", type=Path, default=ROOT / "data")
    p_backup = sub.add_parser("backup")
    p_backup.add_argument("--data", type=Path, default=ROOT / "data")
    p_backup.add_argument("--destination", type=Path, required=True)
    p_backup.add_argument("--offline", action="store_true", required=True, help="confirm every API, worker, compiler and old version is stopped")
    p_verify = sub.add_parser("verify-backup")
    p_verify.add_argument("package", type=Path)
    for name in ("restore", "migrate-copy"):
        item = sub.add_parser(name)
        item.add_argument("package", type=Path)
        item.add_argument("--destination", type=Path, required=True, help="new path that does not exist")
    p_hash = sub.add_parser("source-hashes", help="produce an allowlist for source files reviewed by the organizer")
    p_hash.add_argument("sources", nargs="+", type=Path)
    p_hash.add_argument("--normalize-newlines", action="store_true", help="approve the LF form used by the browser textarea")
    p_release = sub.add_parser("release-manifest")
    _runtime_options(p_release)
    p_release.add_argument("--output", type=Path, required=True)
    p_release.add_argument("--public-origin", default=os.environ.get("CONTEST_PUBLIC_ORIGIN"))
    p_maintenance = sub.add_parser("maintenance")
    p_maintenance.add_argument("--data", type=Path, default=ROOT / "data")
    group = p_maintenance.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", action="store_true")
    group.add_argument("--off", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        if args.cmd == "build-image":
            command = ["docker", "build", "--target", args.target, "-f", str(ROOT / "docker" / "Dockerfile.bot"), "-t", args.image]
            if args.base_image:
                if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", args.base_image):
                    raise ValueError("--base-image must be pinned by a SHA-256 digest.")
                command += ["--build-arg", "BASE_IMAGE=" + args.base_image]
            sys.exit(subprocess.call(command + [str(ROOT / "docker")]))
        if args.cmd == "source-hashes":
            hashes = []
            for path in args.sources:
                raw = path.read_bytes()
                raw.decode("utf-8")
                if args.normalize_newlines:
                    raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                if len(raw) > 256 * 1024 or b"\x00" in raw:
                    raise ValueError("Approved sources must be UTF-8, at most 256 KiB, without NUL.")
                hashes.append(hashlib.sha256(raw).hexdigest())
            print(json.dumps({"source_sha256": sorted(set(hashes))}, indent=2))
            return
        if args.cmd == "backup":
            result = create_backup(args.data, args.destination, offline=args.offline)
        elif args.cmd == "verify-backup":
            result = verify_backup(args.package)
        elif args.cmd == "restore":
            result = restore_backup(args.package, args.destination)
        elif args.cmd == "migrate-copy":
            result = rehearse_migration(args.package, args.destination)
        else:
            result = None
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        args.data.mkdir(parents=True, exist_ok=True)
        if args.cmd == "maintenance":
            set_maintenance(args.data, args.on)
            print(json.dumps({"maintenance": args.on}))
            return
        if args.cmd == "seed":
            with DataDirectoryLock(args.data):
                seed(args.data)
            return
        if not 1 <= args.workers <= 16 or args.move_delay_ms < 0:
            raise ValueError("workers must be 1-16; move delay must be nonnegative.")
        if args.profile == "demo" and args.workers != 1:
            raise ValueError("Demo permits exactly one concurrent match.")
        sandbox = _sandbox(args)
        if args.cmd == "release-manifest":
            manifest = release_manifest(args.data, sandbox, workers=args.workers, public_origin=args.public_origin,
                                        profile=args.profile, move_delay_ms=args.move_delay_ms)
            write_manifest(args.output, manifest)
            print(f"release manifest: {args.output}")
            return
        runner.MOVE_DELAY_S = args.move_delay_ms / 1000
        signal.signal(signal.SIGTERM, _interrupt)
        with DataDirectoryLock(args.data, shared=True), ExitStack() as runtime_locks:
            store = Store(args.data / "ott.db")
            if args.cmd == "worker":
                dispatcher = Dispatcher(store, args.data, workers=args.workers)
                dispatcher.start()
                try:
                    while dispatcher.is_alive():
                        dispatcher.join(timeout=1)
                except KeyboardInterrupt:
                    pass
                finally:
                    dispatcher.shutdown()
                return
            # Private origin binds loopback even when a Tunnel makes it public.
            if args.host not in ("127.0.0.1", "localhost"):
                raise ValueError("API must bind IPv4 loopback. Use the gateway for Internet access.")
            runtime_locks.enter_context(DataDirectoryLock(args.data, lock_name=".api-lock"))
            key = os.environ.get("CONTEST_ORIGIN_KEY")
            old_key = os.environ.get("CONTEST_ORIGIN_KEY_PREVIOUS")
            if bool(args.public_origin) != bool(key):
                raise ValueError("Set both CONTEST_ORIGIN_KEY and PUBLIC_ORIGIN for a public gateway.")
            keys = tuple(value for value in (key, old_key) if value)
            policy = ServerPolicy(public_origin=args.public_origin, origin_keys=keys,
                                  allow_stream=not (args.disable_stream or args.profile == "demo"),
                                  connection_limit=args.connections, dispatch_enabled=not args.no_dispatch,
                                  demo=args.profile == "demo")
            app = make_app(store, args.data, args.web, args.secure_cookie or bool(keys),
                           args.workers, not (args.no_register or args.profile == "demo"), policy=policy)
            server = serve(app, args.host, args.port)
            print(f"Competition API on http://{args.host}:{args.port} ({sandbox.kind})")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (BackupError, InfraError, ValueError, sqlite3.Error, OSError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
