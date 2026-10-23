"""TLS TCP server and persistent submission workers."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import ssl
from pathlib import Path
from typing import Any

try:
    import orjson
except ImportError:
    orjson = None

from judge.database import Database
from core.languages import LANGUAGES
from judge.judge_engine import DockerJudge, JudgeInfrastructureError
from judge.problems import ProblemSet
from judge.protocol import (
    MAX_LINE_BYTES,
    MAX_SOURCE_BYTES,
    PROTOCOL_VERSION,
    RequestError,
    failure,
    success,
    validate_request,
)


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
MAX_PASSWORD_BYTES = 128
MIN_PASSWORD_BYTES = 10
LOG = logging.getLogger("judge.server")


class JudgeService:
    def __init__(self, database: Database, problems: ProblemSet, engine: DockerJudge) -> None:
        self.database = database
        self.problems = problems
        self.engine = engine
        self._worker_tasks: list[asyncio.Task[None]] = []

    def start_workers(self, count: int = 2) -> None:
        self.database.enable_thread_connection_reuse()
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(), name=f"judge-worker-{index + 1}")
            for index in range(count)
        ]

    async def stop_workers(self) -> None:
        for task in self._worker_tasks:
            task.cancel()
        try:
            if self._worker_tasks:
                await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()
        finally:
            self.database.close_thread_connection()

    async def _worker_loop(self) -> None:
        while True:
            submission = self.database.claim_next_submission()
            if submission is None:
                await asyncio.sleep(0.25)
                continue
            try:
                problem = self.problems.get(submission["problem_id"])
                if problem is None:
                    raise JudgeInfrastructureError("Problem package disappeared after submission.")
                result = (await self.engine.judge(problem, submission["source"])
                          if submission["language"] == "cpp17" else
                          await self.engine.judge(problem, submission["source"], submission["language"]))
                self.database.finish_submission(
                    submission["id"],
                    verdict=str(result["verdict"]),
                    score=int(result["score"]),
                    test_results=list(result["test_results"]),
                    message=str(result["message"]) if result.get("message") else None,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Could not judge submission %s", submission["id"])
                self.database.fail_submission(submission["id"], "Judge worker failed. Check server logs.")

    async def handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        connection_tokens: set[str] = set()
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._send(writer, failure(None, "REQUEST_TOO_LARGE", "Request line exceeds 1 MiB."))
                    break
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    await self._send(writer, failure(None, "REQUEST_TOO_LARGE", "Request line exceeds 1 MiB."))
                    break
                if not line.endswith(b"\n"):
                    await self._send(writer, failure(None, "INVALID_FRAMING", "Each request must end with a newline."))
                    break
                try:
                    raw_request = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._send(writer, failure(None, "INVALID_JSON", "Request must be valid UTF-8 JSON."))
                    continue
                request_id = raw_request.get("request_id") if isinstance(raw_request, dict) else None
                try:
                    request = validate_request(raw_request)
                    response = await self.dispatch(request, connection_tokens)
                except RequestError as exc:
                    response = failure(request_id if isinstance(request_id, str) else None, exc.code, exc.message)
                except Exception:
                    LOG.exception("Request failed from %s", peer)
                    response = failure(request_id if isinstance(request_id, str) else None, "INTERNAL_ERROR", "Request failed.")
                await self._send(writer, response)
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass

    async def _send(self, writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
        try:
            payload = orjson.dumps(response) if orjson is not None else None
        except (TypeError, ValueError, OverflowError):
            payload = None
        if payload is None:
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        writer.write(payload + b"\n")
        await writer.drain()

    async def dispatch(
        self,
        request: dict[str, Any],
        connection_tokens: set[str] | None = None,
    ) -> dict[str, Any]:
        if connection_tokens is None:
            connection_tokens = set()
        operation = request["op"]
        request_id = request["request_id"]
        data = request["data"]

        if operation == "REGISTER":
            username = data.get("username")
            password = data.get("password")
            self._validate_credentials(username, password)
            try:
                created = await asyncio.to_thread(self.database.register, username, password)
            except ValueError as exc:
                if str(exc) == "USERNAME_TAKEN":
                    raise RequestError("USERNAME_TAKEN", "Username is already registered.") from exc
                raise
            return success(request_id, created)

        if operation == "LOGIN":
            username = data.get("username")
            password = data.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise RequestError("INVALID_CREDENTIALS", "Username or password is incorrect.")
            try:
                password_size = len(password.encode("utf-8"))
            except UnicodeEncodeError:
                password_size = 0
            if (
                not USERNAME_RE.fullmatch(username)
                or not MIN_PASSWORD_BYTES <= password_size <= MAX_PASSWORD_BYTES
            ):
                raise RequestError("INVALID_CREDENTIALS", "Username or password is incorrect.")
            authenticated = await asyncio.to_thread(self.database.authenticate, username, password)
            if authenticated is None:
                raise RequestError("INVALID_CREDENTIALS", "Username or password is incorrect.")
            user_id, token = authenticated
            connection_tokens.clear()
            connection_tokens.add(token)
            return success(request_id, {"user_id": user_id, "token": token, "expires_in": 43200})

        if operation == "LIST_PROBLEMS":
            return success(request_id, {"problems": self.problems.list_public()})

        if operation == "GET_PROBLEM":
            problem_id = data.get("problem_id")
            if not isinstance(problem_id, str):
                raise RequestError("INVALID_REQUEST", "problem_id must be a string.")
            problem = self.problems.get(problem_id)
            if problem is None:
                raise RequestError("NOT_FOUND", "Problem does not exist.")
            return success(request_id, {"problem": problem.public_data()})

        if operation == "SUBMIT":
            user_id = self._authenticated_user(request.get("token"), connection_tokens)
            problem_id = data.get("problem_id")
            language = data.get("language")
            source = data.get("source")
            if not isinstance(problem_id, str) or not isinstance(source, str):
                raise RequestError("INVALID_REQUEST", "problem_id and source must be strings.")
            if not isinstance(language, str) or language not in LANGUAGES:
                raise RequestError("UNSUPPORTED_LANGUAGE", "Choose " + ", ".join(LANGUAGES) + ".")
            try:
                source_size = len(source.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise RequestError("INVALID_SOURCE", "Source must contain valid Unicode text.") from exc
            if source_size > MAX_SOURCE_BYTES:
                raise RequestError("SOURCE_TOO_LARGE", "Source file exceeds 256 KiB.")
            if "\x00" in source:
                raise RequestError("INVALID_SOURCE", "Source file must not contain NUL characters.")
            if self.problems.get(problem_id) is None:
                raise RequestError("NOT_FOUND", "Problem does not exist.")
            submission_id = self.database.create_submission(user_id, problem_id, source, language)
            return success(request_id, {"submission_id": submission_id, "status": "QUEUED"})

        if operation == "GET_SUBMISSION":
            user_id = self._authenticated_user(request.get("token"), connection_tokens)
            submission_id = data.get("submission_id")
            if not isinstance(submission_id, str):
                raise RequestError("INVALID_REQUEST", "submission_id must be a string.")
            submission = self.database.get_submission(user_id, submission_id)
            if submission is None:
                raise RequestError("NOT_FOUND", "Submission does not exist.")
            return success(request_id, {"submission": submission})

        if operation == "LIST_MY_SUBMISSIONS":
            user_id = self._authenticated_user(request.get("token"), connection_tokens)
            limit = data.get("limit", 20)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise RequestError("INVALID_REQUEST", "limit must be an integer from 1 to 100.")
            return success(request_id, {"submissions": self.database.list_submissions(user_id, limit)})

        raise RequestError("INVALID_REQUEST", "Unsupported operation.")

    @staticmethod
    def _validate_credentials(username: Any, password: Any) -> None:
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise RequestError("INVALID_USERNAME", "Username must contain 3–32 letters, digits, or underscores.")
        if not isinstance(password, str):
            raise RequestError("INVALID_PASSWORD", "Password must be text.")
        try:
            password_bytes = password.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RequestError("INVALID_PASSWORD", "Password must be valid UTF-8 text.") from exc
        if not MIN_PASSWORD_BYTES <= len(password_bytes) <= MAX_PASSWORD_BYTES:
            raise RequestError("INVALID_PASSWORD", "Password must be 10–128 UTF-8 bytes.")

    def _authenticated_user(self, token: Any, connection_tokens: set[str]) -> int:
        if not isinstance(token, str) or not token:
            raise RequestError("AUTH_REQUIRED", "Log in before using this operation.")
        if token not in connection_tokens:
            raise RequestError("AUTH_REQUIRED", "Log in on this connection before using this operation.")
        user_id = self.database.user_for_token(token)
        if user_id is None:
            raise RequestError("AUTH_REQUIRED", "Session is invalid or expired.")
        return user_id


def make_tls_context(cert_file: str | Path, key_file: str | Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_file), str(key_file))
    return context


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LAN programming judge server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", default="data/judge.sqlite3")
    parser.add_argument("--problems", default="problems")
    parser.add_argument("--tls-cert", default="data/server.crt")
    parser.add_argument("--tls-key", default="data/server.key")
    parser.add_argument("--docker-image", default="lan-judge-cpp17")
    parser.add_argument("--docker-runtime-image", default="lan-judge-runtime")
    return parser


async def serve(args: argparse.Namespace) -> None:
    cert = Path(args.tls_cert)
    key = Path(args.tls_key)
    if not cert.is_file() or not key.is_file():
        raise SystemExit("TLS certificate and key must exist. See README.md for local certificate setup.")
    if not 1 <= args.port <= 65535:
        raise SystemExit("Port must be between 1 and 65535.")
    try:
        ssl_context = make_tls_context(cert, key)
        database = Database(args.database)
        problems = ProblemSet(args.problems)
        engine = DockerJudge(args.docker_image, args.docker_runtime_image)
        await engine.check_available()
        if engine.compiler_image_id == engine.runtime_image_id:
            raise JudgeInfrastructureError("Server requires separate compiler and runtime images.")
    except (OSError, ValueError, ssl.SSLError, JudgeInfrastructureError) as exc:
        raise SystemExit(str(exc)) from exc

    service = JudgeService(database, problems, engine)
    service.start_workers(count=2)
    server = await asyncio.start_server(
        service.handle_connection,
        host=args.host,
        port=args.port,
        ssl=ssl_context,
        limit=MAX_LINE_BYTES + 1,
    )
    sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    LOG.info("Listening with TLS on %s", sockets)
    try:
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        await service.stop_workers()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args()
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

