"""Command-line client for the TLS TCP judge protocol."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import ssl
import sys
import uuid
from pathlib import Path
from typing import Any

from core.languages import LANGUAGES
from judge.protocol import MAX_LINE_BYTES, MAX_SOURCE_BYTES, PROTOCOL_VERSION


class ClientError(RuntimeError):
    pass


class JudgeClient:
    def __init__(self, host: str, port: int, ca_cert: str) -> None:
        self.host = host
        self.port = port
        self.ca_cert = ca_cert
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> "JudgeClient":
        context = ssl.create_default_context(cafile=self.ca_cert)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.reader, self.writer = await asyncio.open_connection(
            self.host,
            self.port,
            ssl=context,
            server_hostname=self.host,
            limit=MAX_LINE_BYTES + 1,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()

    async def request(
        self,
        operation: str,
        data: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        if self.reader is None or self.writer is None:
            raise ClientError("Client connection is not open.")
        request_id = uuid.uuid4().hex
        request = {
            "version": PROTOCOL_VERSION,
            "request_id": request_id,
            "op": operation,
            "token": token,
            "data": data or {},
        }
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_LINE_BYTES:
            raise ClientError("Request exceeds 1 MiB.")
        self.writer.write(encoded)
        await self.writer.drain()
        line = await self.reader.readline()
        if not line:
            raise ClientError("Server closed connection without a response.")
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientError("Server returned invalid JSON.") from exc
        if response.get("request_id") != request_id:
            raise ClientError("Response request_id does not match.")
        if response.get("ok") is not True:
            error = response.get("error", {})
            raise ClientError(f"{error.get('code', 'SERVER_ERROR')}: {error.get('message', 'Request failed.')}")
        result = response.get("data")
        if not isinstance(result, dict):
            raise ClientError("Server response data must be an object.")
        return result

    async def login(self, username: str, password: str) -> str:
        result = await self.request("LOGIN", {"username": username, "password": password})
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise ClientError("Server did not return a session token.")
        return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit C++17 code to a LAN judge.")
    parser.add_argument("--host", default="127.0.0.1", help="TLS server host")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ca-cert", default="data/server.crt", help="PEM certificate used to verify server")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("problems", help="list available problems")
    show = commands.add_parser("show", help="show problem statement and sample")
    show.add_argument("problem_id")

    register = commands.add_parser("register", help="create an account")
    register.add_argument("--username", required=True)

    login = commands.add_parser("login", help="verify account credentials")
    login.add_argument("--username", required=True)

    submit = commands.add_parser("submit", help="submit one native source file")
    submit.add_argument("problem_id")
    submit.add_argument("source", type=Path)
    submit.add_argument("--language", choices=tuple(LANGUAGES), default="cpp17")
    submit.add_argument("--username", required=True)

    result = commands.add_parser("result", help="read submission status or result")
    result.add_argument("submission_id")
    result.add_argument("--username", required=True)
    result.add_argument("--follow", action="store_true", help="poll until judging finishes")

    history = commands.add_parser("history", help="list your latest submissions")
    history.add_argument("--username", required=True)
    history.add_argument("--limit", type=int, default=20)
    return parser


def _password(prompt: str = "Password: ") -> str:
    return getpass.getpass(prompt)


async def run_command(args: argparse.Namespace) -> dict[str, Any]:
    async with JudgeClient(args.host, args.port, args.ca_cert) as client:
        if args.command == "problems":
            return await client.request("LIST_PROBLEMS")
        if args.command == "show":
            return await client.request("GET_PROBLEM", {"problem_id": args.problem_id})
        if args.command == "register":
            password = _password()
            confirmation = _password("Confirm password: ")
            if password != confirmation:
                raise ClientError("Passwords do not match.")
            created = await client.request(
                "REGISTER",
                {"username": args.username, "password": password},
            )
            return {"account": created}
        if args.command == "login":
            password = _password()
            token = await client.login(args.username, password)
            return {"user": args.username, "authenticated": bool(token)}
        if args.command == "submit":
            try:
                source = args.source.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ClientError(f"Could not read source file: {exc}") from exc
            if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                raise ClientError("Source file exceeds 256 KiB.")
            password = _password()
            token = await client.login(args.username, password)
            return await client.request(
                "SUBMIT",
                {"problem_id": args.problem_id, "language": args.language, "source": source},
                token,
            )
        if args.command == "result":
            password = _password()
            token = await client.login(args.username, password)
            while True:
                result = await client.request(
                    "GET_SUBMISSION",
                    {"submission_id": args.submission_id},
                    token,
                )
                submission = result.get("submission", {})
                if not args.follow or submission.get("status") in {"FINISHED", "SYSTEM_ERROR"}:
                    return result
                await asyncio.sleep(1)
        if args.command == "history":
            password = _password()
            token = await client.login(args.username, password)
            return await client.request("LIST_MY_SUBMISSIONS", {"limit": args.limit}, token)
    raise ClientError("Unsupported command.")


def main() -> None:
    args = build_parser().parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("Port must be between 1 and 65535.")
    if args.command == "history" and not 1 <= args.limit <= 100:
        raise SystemExit("History limit must be between 1 and 100.")
    if not Path(args.ca_cert).is_file():
        raise SystemExit("CA certificate not found. Pass --ca-cert with server certificate PEM.")
    try:
        result = asyncio.run(run_command(args))
    except (ClientError, OSError, ssl.SSLError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

