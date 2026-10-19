"""CLI: `python -m ott serve|seed`."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import subprocess
import sys
from pathlib import Path

from ott import runner
from ott.sandbox import IMAGE, make_sandbox
from ott.api import make_app, serve
from ott.store import Store

ROOT = Path(__file__).resolve().parent.parent
SEED_USERS = [("organizer", "ORGANIZER"), ("team_alpha", "CONTESTANT"), ("team_beta", "CONTESTANT"),
              ("team_gamma", "CONTESTANT"), ("team_delta", "CONTESTANT")]


def seed(data_dir: Path) -> None:
    """Create the demo accounts. Passwords are generated once and written to data/seed-accounts.json."""
    store = Store(data_dir / "ott.db")
    creds_path = data_dir / "seed-accounts.json"
    creds = json.loads(creds_path.read_text(encoding="utf-8")) if creds_path.exists() else {}
    for username, role in SEED_USERS:
        if store.get_user(username):
            store.set_role(username, role)
            continue
        password = secrets.token_urlsafe(12)
        store.create_user(username, password, role)
        creds[username] = {"role": role, "password": password}
        print(f"created {role.lower()} account: {username}")
    creds_path.write_text(json.dumps(creds, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"credentials file: {creds_path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ott")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="run API + web client")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--data", type=Path, default=ROOT / "data")
    p_serve.add_argument("--web", type=Path, default=ROOT / "mockup")
    p_serve.add_argument("--workers", type=int, default=4, help="max concurrent matches")
    p_serve.add_argument("--move-delay-ms", type=int, default=2000, help="pause after each move so spectators can follow; not charged to bot clocks (0 = off)")
    p_serve.add_argument("--runner", choices=("auto", "docker", "local"), default="auto", help="where bots compile/run: docker sandbox, plain host processes (dev only), or auto = docker when available")
    p_serve.add_argument("--image", default=IMAGE, help="Docker image for the docker runner")
    p_serve.add_argument("--no-register", action="store_true", help="disable public sign-up (use when exposed to a LAN: dev runner executes submitted bots unsandboxed)")
    p_serve.add_argument("--secure-cookie", action="store_true", help="set Secure on the session cookie (use behind HTTPS)")
    p_build = sub.add_parser("build-image", help="build the Docker image used by the docker runner")
    p_build.add_argument("--image", default=IMAGE)
    p_seed = sub.add_parser("seed", help="create demo organizer + contestant accounts")
    p_seed.add_argument("--data", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.cmd == "seed":
        seed(args.data)
        return
    if args.cmd == "build-image":
        sys.exit(subprocess.call(["docker", "build", "-f", str(ROOT / "docker" / "Dockerfile.bot"), "-t", args.image, str(ROOT / "docker")]))
    sandbox = make_sandbox(args.runner, args.image)
    ok, detail = sandbox.available()
    if not ok:
        sys.exit(f"runner '{sandbox.kind}' unavailable: {detail}")
    runner.set_sandbox(sandbox)
    print(f"runner: {sandbox.kind} ({detail})")
    if sandbox.kind == "local" and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: local runner executes submitted bots as ordinary host processes (no sandbox). "
              "Do not expose this server to untrusted users; use --runner docker.")
    args.data.mkdir(parents=True, exist_ok=True)
    runner.MOVE_DELAY_S = args.move_delay_ms / 1000
    app = make_app(Store(args.data / "ott.db"), args.data, args.web, args.secure_cookie, args.workers, not args.no_register)
    server = serve(app, args.host, args.port)
    print(f"OTT Arena on http://{args.host}:{args.port}  (data: {args.data})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
