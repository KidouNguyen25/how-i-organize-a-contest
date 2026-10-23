"""HTTP gateway: JSON API under /api/v1 plus static web client."""

from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import logging
import math
import mimetypes
import re
import shutil
import socket
import sqlite3
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from contextlib import closing, contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from core.languages import LANGUAGES, build_key, language_profile
from ott import runner
from ott.adapters import get_adapter
from ott.operations import maintenance_enabled, readiness, set_maintenance
from ott.security import RateLimiter, RequestRejected, ServerPolicy
from ott.sandbox import SourcePolicyError
from ott.events import HUB
from ott.runner import LIVE, LIVE_LOCK, InfraError, artifact_path, compile_source
from ott.store import LeaseLost, Store, StoreError
from ott.tournament import Dispatcher, byes, iter_round_robin_rows, standings

LOG = logging.getLogger("ott.api")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
MAX_BODY = 1024 * 1024
MAX_SOURCE = 256 * 1024
MAX_JSON_DEPTH = 64
_JSON_MARKERS = re.compile(rb'["\\{}\[\]]')
_JSON_STRING = re.compile(rb'"[^"\\]*+(?:\\.[^"\\]*+)*+"', re.DOTALL)
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "samples" / "bot_template.cpp"
CODING_MIN, CODING_MAX = 1, 240
COMPILE_LIMIT = 10
CLOCKS = (300, 600, 3600)
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
STATIC_CACHE_MAX_FILE = 1024 * 1024
STATIC_CACHE_MAX_BYTES = 8 * 1024 * 1024
STATIC_CACHE_MAX_ENTRIES = 64
MATCH_VIEW_CACHE_MAX_STATE = 256 * 1024
MATCH_VIEW_CACHE_MAX_BYTES = 2 * 1024 * 1024
MATCH_VIEW_CACHE_MAX_ENTRIES = 64


class StaticFileCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[tuple[int, ...], bytes, str]] = OrderedDict()
        self._size = 0

    def read(self, path: Path) -> tuple[bytes, str]:
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        key = str(path)
        with self._lock:
            cached = self._entries.get(key)
            if cached and cached[0] == signature:
                self._entries.move_to_end(key)
                return cached[1], cached[2]
            if cached:
                del self._entries[key]
                self._size -= len(cached[1])

        data = path.read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest() + '"'
        if len(data) <= STATIC_CACHE_MAX_FILE:
            with self._lock:
                cached = self._entries.get(key)
                if cached and cached[0] == signature:
                    self._entries.move_to_end(key)
                    return cached[1], cached[2]
                if cached:
                    del self._entries[key]
                    self._size -= len(cached[1])
                self._entries[key] = (signature, data, etag)
                self._size += len(data)
                while self._size > STATIC_CACHE_MAX_BYTES or len(self._entries) > STATIC_CACHE_MAX_ENTRIES:
                    _, evicted = self._entries.popitem(last=False)
                    self._size -= len(evicted[1])
        return data, etag


class MatchViewCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, bool], tuple[tuple[str, str, int], dict[str, Any], int]] = OrderedDict()
        self._size = 0

    def get(self, match_id: str, game_id: str, game_version: str, state_version: int,
            *, with_moves: bool = True) -> dict[str, Any] | None:
        key = (game_id, game_version, state_version)
        cache_id = (match_id, with_moves)
        with self._lock:
            cached = self._entries.get(cache_id)
            if not cached:
                return None
            if cached[0] == key:
                self._entries.move_to_end(cache_id)
                return cached[1]
            if cached[0][2] > state_version:
                return None
            del self._entries[cache_id]
            self._size -= cached[2]
            return None

    def put(self, match_id: str, game_id: str, game_version: str, state_version: int,
            view: dict[str, Any], weight: int, *, with_moves: bool = True) -> dict[str, Any]:
        if weight > MATCH_VIEW_CACHE_MAX_STATE or weight > MATCH_VIEW_CACHE_MAX_BYTES:
            return view
        key = (game_id, game_version, state_version)
        cache_id = (match_id, with_moves)
        with self._lock:
            cached = self._entries.get(cache_id)
            if cached and cached[0] == key:
                self._entries.move_to_end(cache_id)
                return cached[1]
            if cached and cached[0][2] > state_version:
                return view
            if cached:
                del self._entries[cache_id]
                self._size -= cached[2]
            self._entries[cache_id] = (key, view, weight)
            self._size += weight
            while self._size > MATCH_VIEW_CACHE_MAX_BYTES or len(self._entries) > MATCH_VIEW_CACHE_MAX_ENTRIES:
                _, evicted = self._entries.popitem(last=False)
                self._size -= evicted[2]
        return view


def _json_string_end(raw: bytes, start: int) -> int:
    """Skip ordinary strings with native searches; bound probes before regex fallback."""
    position = start + 1
    for _ in range(8):
        end = raw.find(b'"', position)
        if end < 0:
            raise ApiError(400, "BAD_REQUEST", "Body must be valid JSON.")
        back = end - 1
        while back > start and raw[back] == 92 and end - back <= 8:
            back -= 1
        if end - back > 8:
            break
        if (end - back) % 2:
            return end + 1
        position = end + 1
    string = _JSON_STRING.match(raw, start)
    if string is None:
        raise ApiError(400, "BAD_REQUEST", "Body must be valid JSON.")
    return string.end()


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def make_app(store: Store, data_dir: Path, static_dir: Path, secure_cookie: bool = False, workers: int = 1,
             open_registration: bool = True, *, policy: ServerPolicy | None = None) -> "App":
    app = App(store, data_dir, static_dir, secure_cookie, workers, policy or ServerPolicy())
    app.open_registration = open_registration and not app.policy.demo
    return app


class App:
    def __init__(self, store: Store, data_dir: Path, static_dir: Path, secure_cookie: bool, workers: int,
                 policy: ServerPolicy | None = None) -> None:
        self.store, self.data_dir, self.static_dir, self.secure_cookie = store, data_dir, static_dir, secure_cookie
        self.static_cache = StaticFileCache()
        self.match_view_cache = MatchViewCache()
        self.policy = policy or ServerPolicy()
        self.secure_cookie = secure_cookie or bool(self.policy.origin_keys)
        if self.policy.demo:
            info = runner.SANDBOX.policy_info()
            if info["kind"] != "docker" or info["source_policy"] != "allowlist" or not info["allowed_source_count"]:
                raise ValueError("Demo requires Docker and a nonempty approved source allowlist.")
            if not info["runtime_image_id"] or info["runtime_image_id"] == info["compiler_image_id"]:
                raise ValueError("Demo requires separate verified compiler and runtime images.")
        self.dispatcher = Dispatcher(store, data_dir, workers)
        self.compile_slots = threading.BoundedSemaphore(1)
        self.stop_event = threading.Event()
        self.intake_lock = threading.Lock()
        self.intakes: dict[tuple[str, int], int] = {}
        self.watcher: threading.Thread | None = None
        self.rate_limiter = RateLimiter()
        self.auth_limiter = RateLimiter()
        self.login_limiter = RateLimiter()
        self.account_limiter = RateLimiter()
        self.compile_check_limiter = RateLimiter()
        self.open_registration = True
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Any], str]] = []
        self.routes_by_prefix: dict[
            str, dict[str, list[tuple[str, re.Pattern[str], Callable[..., Any], str]]]
        ] = {}
        self._register_routes()

    def start(self) -> None:
        if self.policy.dispatch_enabled:
            self.dispatcher.start()
        self.watcher = threading.Thread(target=self._coding_watcher, daemon=True, name="coding-watcher")
        self.watcher.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.dispatcher.shutdown()
        if self.watcher:
            while self.watcher.is_alive():
                self.watcher.join(timeout=1)

    def check_source(self, source: str) -> None:
        try:
            runner.SANDBOX.check_source(source)
        except SourcePolicyError as exc:
            raise ApiError(403, "SOURCE_NOT_APPROVED", str(exc)) from None

    def health_report(self) -> dict[str, Any]:
        report = readiness(self.store, self.data_dir, runner.SANDBOX)
        report["checks"]["serving"] = not self.stop_event.is_set()
        report["checks"]["submission_worker"] = bool(self.watcher and self.watcher.is_alive())
        if self.policy.dispatch_enabled:
            report["checks"]["match_dispatcher"] = self.dispatcher.is_alive()
        report["ready"] = all(report["checks"].values())
        return report

    @contextmanager
    def build_slot(self):
        if self.stop_event.is_set() or maintenance_enabled(self.data_dir):
            raise InfraError("Maintenance is active.")
        if not readiness(self.store, self.data_dir, runner.SANDBOX)["ready"]:
            raise InfraError("Compiler admission is paused until runtime and storage are ready.")
        if not self.compile_slots.acquire(blocking=False):
            raise InfraError("Compiler is busy; retry later.")
        try:
            owner = uuid.uuid4().hex
            with self.store.compile_slot(owner, limit=1, lease_seconds=180) as token:
                yield owner, token
        except LeaseLost as exc:
            raise InfraError("Compiler lease unavailable or lost; retry later.") from exc
        finally:
            self.compile_slots.release()

    def template_source(self, language: str = "cpp17") -> str:
        profile = language_profile(language)
        path = TEMPLATE_PATH if language == "cpp17" else TEMPLATE_PATH.with_name("bot_template" + Path(profile.filename).suffix)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            raise ApiError(503, "TEMPLATE_UNAVAILABLE", "Bot template is unavailable.") from None

    def compile_final(self, bot_id: str, source: str, language: str = "cpp17") -> tuple[bool, str | None]:
        self.check_source(source)
        target = artifact_path(self.data_dir, bot_id)
        ok, msg = compile_source(target, source) if language == "cpp17" else compile_source(target, source, language)
        if not ok:
            shutil.rmtree(artifact_path(self.data_dir, bot_id).parent, ignore_errors=True)
        return ok, msg

    def check_dir(self, entry_id: str) -> Path:
        return artifact_path(self.data_dir, f"check-{entry_id}").parent

    def cached_build(self, entry_id: str, source: str, language: str = "cpp17") -> Path | None:
        sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
        cached = artifact_path(self.data_dir, f"check-{entry_id}-{build_key(sha, language)}")
        if not cached.exists():
            return None
        runner.SANDBOX.verify_artifact(cached, expected_source_hash=sha, expected_language=language)
        return cached

    def build_final(self, entry_id: str, bot_id: str, source: str, language: str = "cpp17") -> tuple[bool, str | None]:
        """Build an accepted receipt; quota is charged by the fenced caller."""
        self.check_source(source)
        sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
        dest = artifact_path(self.data_dir, bot_id)
        if dest.exists():
            runner.SANDBOX.verify_artifact(dest, expected_source_hash=sha, expected_language=language)
            return True, None
        cached = self.cached_build(entry_id, source, language)
        if cached is not None:
            runner.SANDBOX.copy_artifact(cached, dest, expected_source_hash=sha, expected_language=language)
            return True, None
        return self.compile_final(bot_id, source) if language == "cpp17" else self.compile_final(bot_id, source, language)

    def _process_submission(self, receipt: dict[str, Any]) -> None:
        charged = False
        with self.build_slot() as compile_lease:
            # Another API can have completed this receipt before the slot was free.
            current = self.store.get_pending_submission(receipt["entry_id"])
            if not current or current["id"] != receipt["id"] or current["status"] != "QUEUED":
                return
            try:
                self.check_source(receipt["source"])
                if not receipt["runtime_profile"] or json.loads(receipt["runtime_profile"]) != runner.SANDBOX.policy_info():
                    raise InfraError("Runtime profile differs from the accepted submission.")
                entry = self.store.get_entry(receipt["entry_id"])
                if not entry:
                    raise InfraError("Accepted entry is missing.")
                dest = artifact_path(self.data_dir, receipt["bot_id"])
                if not receipt["free_compile"] and self.cached_build(receipt["entry_id"], receipt["source"], receipt["language"]) is None and not dest.exists():
                    self.store.bump_compile(receipt["entry_id"], COMPILE_LIMIT, receipt_id=receipt["id"])
                    charged = True
                ok, message = self.build_final(receipt["entry_id"], receipt["bot_id"], receipt["source"], receipt["language"])
                if not ok:
                    self.store.fail_submission(receipt["id"], message or "COMPILE_ERROR", compile_lease=compile_lease)
                    return
                self.store.finalize_entry(receipt["entry_id"], receipt["tournament_id"], receipt["user_id"],
                        receipt["bot_id"], receipt["source"], compile_lease=compile_lease, language=receipt["language"])
            except (InfraError, LeaseLost, OSError, ApiError, sqlite3.Error, StoreError) as exc:
                try:
                    if charged:
                        self.store.refund_compile(receipt["entry_id"], receipt_id=receipt["id"], compile_lease=compile_lease)
                    self.store.fail_submission(receipt["id"], "INFRA: " + type(exc).__name__,
                                               infra=True, compile_lease=compile_lease)
                except LeaseLost:
                    # Keep the immutable receipt for the next valid owner.
                    LOG.warning("build receipt %s lost its lease", receipt["id"])

    def _coding_watcher(self) -> None:
        self.store.enable_thread_connection_reuse()
        try:
            self._coding_watcher_loop()
        finally:
            self.store.close_thread_connection()

    def _coding_watcher_loop(self) -> None:
        """Process durable final submissions and reserve missed-deadline drafts."""
        while not self.stop_event.is_set():
            try:
                if maintenance_enabled(self.data_dir):
                    self.stop_event.wait(1.0)
                    continue
                for row in self.store.expired_coding():
                    if self.stop_event.is_set():
                        break
                    with self.intake_lock:
                        if self.intakes.get((row["tid"], row["user_id"]), 0):
                            continue
                        if not (row["draft_source"] or "").strip():
                            self.store.set_submit_note(row["eid"], "Hết giờ: chưa có mã nguồn để nộp.")
                            continue
                        try:
                            self.check_source(row["draft_source"])
                            self.store.reserve_submission(row["eid"], row["tid"], row["user_id"],
                                                          row["draft_source"], time.time(), auto=True, language=row["draft_language"],
                                                          runtime_profile=runner.SANDBOX.policy_info())
                        except (ApiError, StoreError) as exc:
                            self.store.set_submit_note(row["eid"], exc.message)
                for receipt in self.store.queued_submissions():
                    if self.stop_event.is_set() or maintenance_enabled(self.data_dir):
                        break
                    try:
                        self._process_submission(receipt)
                    except InfraError:
                        break
            except Exception:
                LOG.exception("submission watcher failed")
            self.stop_event.wait(1.0)

    # ---------- serializers ----------
    def tournament_json(self, t: dict[str, Any]) -> dict[str, Any]:
        keys = ("id", "name", "game_id", "game_version", "clock_seconds", "status", "created_at", "started_at", "finished_at",
                "entries_count", "ready_count", "matches_total", "matches_finished", "matches_running",
                "coding_seconds", "coding_started_at", "coding_deadline")
        out = {k: t[k] for k in keys}
        out["server_time"] = int(time.time())
        out["schedule_created"] = t["matches_total"] > 0
        return out

    def entry_json(self, e: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": e["id"], "tournament_id": e["tournament_id"], "username": e["username"], "status": e["status"],
            "registered_at": e["registered_at"], "bot_version_id": e["bot_submission_id"], "compile_status": e["compile_status"],
            "submitted_at": e["submitted_at"], "submit_note": e["submit_note"],
            "submission_status": e.get("submission_status"),
        }

    def bot_json(self, b: dict[str, Any], selected: str | None = None) -> dict[str, Any]:
        return {
            "id": b["id"], "tournament_id": b["tournament_id"], "compile_status": b["compile_status"], "compile_message": b["compile_message"],
            "created_at": b["created_at"], "size": b["source_size"], "sha256": b["source_sha256"], "selected": b["id"] == selected,
        }

    def summary(self, m: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": m["id"], "tournament_id": m["tournament_id"], "tournament_name": m["tournament_name"],
            "round_no": m["round_no"], "leg_no": m["leg_no"], "pairing_no": m["pairing_no"],
            "white": {"entry_id": m["white_entry_id"], "username": m["white_name"]},
            "black": {"entry_id": m["black_entry_id"], "username": m["black_name"]},
            "status": m["status"], "result": m["result"], "result_reason": m["result_reason"],
            "phase": m["phase"], "move_count": m["move_count"], "side_to_act": m["side_to_act"],
        }

    def live_clocks(self, m: dict[str, Any]) -> tuple[dict[str, int], str | None]:
        clocks = {"white": m["white_clock_ms"], "black": m["black_clock_ms"]}
        if m["status"] != "RUNNING":
            return clocks, None
        with LIVE_LOCK:
            live = LIVE.get(m["id"])
            live = dict(live) if live else None
        if not live:
            return clocks, m["side_to_act"].lower() if m["side_to_act"] else None
        clocks = dict(live["clocks"])
        if live["side"] is None:  # pacing pause between moves
            return clocks, None
        clocks[live["side"]] = max(0, int(clocks[live["side"]] - (time.monotonic() - live["since"]) * 1000))
        return clocks, live["side"]

    def cached_view(self, m: dict[str, Any], *, with_moves: bool) -> dict[str, Any] | None:
        version = m.get("state_version")
        if type(version) is not int:
            return None
        return self.match_view_cache.get(m["id"], m["game_id"], m["game_version"], version,
                                         with_moves=with_moves)

    def detail(self, m: dict[str, Any], with_moves: bool = True) -> dict[str, Any]:
        version = m.get("state_version")
        view = self.cached_view(m, with_moves=with_moves)
        if view is None:
            if "state_json" not in m:
                full_match = self.store.get_match_render(m["id"])
                if full_match is None:
                    raise ApiError(404, "NOT_FOUND", "Match not found.")
                m = full_match
                version = m.get("state_version")
                view = self.cached_view(m, with_moves=with_moves)
            state_json = m.get("state_json")
            state_zlib = m.get("state_zlib")
        else:
            state_json = None
            state_zlib = None
        if view is None:
            state_bytes = zlib.decompress(state_zlib) if state_zlib is not None else None
            if state_bytes is not None:
                state = json.loads(state_bytes)
                weight = len(state_bytes)
                has_state = True
            elif state_json:
                state = json.loads(state_json)
                weight = len(state_json.encode("utf-8"))
                has_state = True
            else:
                state = None
                weight = 0
                has_state = False
            public_view = get_adapter(m["game_id"], m["game_version"]).public_view(state)
            if with_moves:
                view = public_view
            else:
                moves = public_view.get("moves", [])
                view = {key: value for key, value in public_view.items() if key != "moves"}
                view["last_move"] = {"from": moves[-1]["from"], "to": moves[-1]["to"]} if moves else None
                weight = len(json.dumps(view, separators=(",", ":")).encode("utf-8"))
            if has_state and type(version) is int and weight <= MATCH_VIEW_CACHE_MAX_STATE:
                view = self.match_view_cache.put(m["id"], m["game_id"], m["game_version"], version, view, weight,
                                                 with_moves=with_moves)
        view = dict(view)
        out = self.summary(m)
        clocks, running = self.live_clocks(m)
        out["clocks_ms"] = clocks
        out["running_side"] = running
        out["clock_seconds"] = m["clock_seconds"]
        out["state_version"] = m.get("state_version")
        if not with_moves:
            out["last_move"] = view.pop("last_move", None)
        out.update(view)
        return out

    # ---------- auth helpers ----------
    def require_login(self, ctx: "Ctx") -> dict[str, Any]:
        if not ctx.user:
            raise ApiError(401, "AUTH_REQUIRED", "Log in first.")
        return ctx.user

    def require_organizer(self, ctx: "Ctx") -> dict[str, Any]:
        user = self.require_login(ctx)
        if user["role"] != "ORGANIZER":
            raise ApiError(403, "FORBIDDEN", "Organizer role required.")
        return user

    def get_tournament(self, tid: str) -> dict[str, Any]:
        t = self.store.get_tournament(tid)
        if not t:
            raise ApiError(404, "NOT_FOUND", "Tournament not found.")
        return t

    def require_tournament(self, tid: str) -> None:
        if not self.store.tournament_exists(tid):
            raise ApiError(404, "NOT_FOUND", "Tournament not found.")

    def get_tournament_status(self, tid: str) -> str:
        status = self.store.get_tournament_status(tid)
        if status is None:
            raise ApiError(404, "NOT_FOUND", "Tournament not found.")
        return status

    # ---------- routes ----------
    def route(self, method: str, pattern: str, mutating: bool | None = None) -> Callable:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def deco(fn: Callable) -> Callable:
            route = (method, regex, fn, "mut" if (mutating if mutating is not None else method != "GET") else "read")
            self.routes.append(route)
            prefix = pattern.lstrip("/").split("/", 1)[0]
            self.routes_by_prefix.setdefault(prefix, {}).setdefault(method, []).append(route)
            return fn

        return deco

    def _register_routes(self) -> None:
        r = self.route
        app = self

        @r("POST", "/auth/register")
        def register(ctx: Ctx) -> Any:
            if not app.open_registration:
                raise ApiError(403, "REGISTRATION_CLOSED", "Public registration is disabled on this server. Ask the organizer for an account.")
            body = ctx.json()
            username, password = body.get("username"), body.get("password")
            if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
                raise ApiError(400, "BAD_REQUEST", "Username must be 3-32 chars: letters, digits, underscore.")
            if not isinstance(password, str) or not 10 <= len(password.encode("utf-8")) <= 128:
                raise ApiError(400, "BAD_REQUEST", "Password must be 10-128 bytes.")
            try:
                user = app.store.create_user(username, password)
            except StoreError as exc:
                raise ApiError(exc.status, exc.code, exc.message) from None
            return 201, {"username": user["username"], "role": user["role"]}

        @r("POST", "/auth/login")
        def login(ctx: Ctx) -> Any:
            body = ctx.json()
            username, password = body.get("username"), body.get("password")
            if not isinstance(username, str) or not USERNAME_RE.fullmatch(username) or not isinstance(password, str):
                raise ApiError(400, "BAD_REQUEST", "username and password are required.")
            try:
                valid_password = 1 <= len(password.encode("utf-8")) <= 128
            except UnicodeError:
                valid_password = False
            if not valid_password:
                raise ApiError(400, "BAD_REQUEST", "Password must be valid UTF-8 and at most 128 bytes.")
            if not app.login_limiter.allow("login:" + username.lower(), burst=10, per_second=1/30):
                raise ApiError(429, "RATE_LIMITED", "Too many attempts. Try again later.")
            user = app.store.verify_login(username, password)
            if not user:
                raise ApiError(401, "INVALID_CREDENTIALS", "Wrong username or password.")
            token, csrf = app.store.create_session(user["id"])
            ctx.set_session = token
            return 200, {"user": {"username": user["username"], "role": user["role"]}, "csrf_token": csrf}

        @r("POST", "/auth/logout")
        def logout(ctx: Ctx) -> Any:
            app.require_login(ctx)
            if ctx.session_token:
                app.store.delete_session(ctx.session_token)
            ctx.clear_session = True
            return 204, None

        @r("GET", "/me")
        def me(ctx: Ctx) -> Any:
            user = app.require_login(ctx)
            return 200, {"username": user["username"], "role": user["role"], "csrf_token": user["csrf_token"]}

        @r("GET", "/stats")
        def stats(ctx: Ctx) -> Any:
            return 200, app.store.site_stats() | {"stream_clients": HUB.clients}

        @r("GET", "/ops/status")
        def operation_status(ctx: Ctx) -> Any:
            app.require_organizer(ctx)
            return 200, {"maintenance": maintenance_enabled(app.data_dir),
                         "workers": app.store.worker_metrics(),
                         "readiness": app.health_report()}

        @r("POST", "/ops/maintenance")
        def operation_maintenance(ctx: Ctx) -> Any:
            user = app.require_organizer(ctx)
            enabled = ctx.json().get("enabled")
            if not isinstance(enabled, bool):
                raise ApiError(400, "BAD_REQUEST", "enabled must be boolean.")
            set_maintenance(app.data_dir, enabled)
            app.store.audit(user["id"], "operations.maintenance", str(enabled))
            return 200, {"maintenance": enabled}

        @r("GET", "/tournaments")
        def list_tournaments(ctx: Ctx) -> Any:
            return 200, {"items": [app.tournament_json(t) for t in app.store.list_tournaments()]}

        @r("POST", "/tournaments")
        def create_tournament(ctx: Ctx) -> Any:
            user = app.require_organizer(ctx)
            body = ctx.json()
            name, clock = body.get("name"), body.get("clock_seconds")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                raise ApiError(400, "BAD_REQUEST", "Name must be 1-80 characters.")
            if clock not in CLOCKS or isinstance(clock, bool):
                raise ApiError(400, "BAD_REQUEST", "clock_seconds must be 300, 600 or 3600.")
            minutes = body.get("coding_minutes", 60)
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not CODING_MIN <= minutes <= CODING_MAX:
                raise ApiError(400, "BAD_REQUEST", f"coding_minutes must be {CODING_MIN}-{CODING_MAX}.")
            t = app.store.create_tournament(name.strip(), clock, user["id"], minutes * 60)
            app.store.audit(user["id"], "tournament.create", t["id"])
            return 201, app.tournament_json(t)

        @r("GET", "/tournaments/{tid}")
        def get_tournament(ctx: Ctx, tid: str) -> Any:
            return 200, app.tournament_json(app.get_tournament(tid))

        @r("PATCH", "/tournaments/{tid}")
        def patch_tournament(ctx: Ctx, tid: str) -> Any:
            user = app.require_organizer(ctx)
            if app.get_tournament_status(tid) != "DRAFT":
                raise ApiError(409, "CONFLICT", "Only DRAFT tournaments can be edited.")
            body = ctx.json()
            fields: dict[str, Any] = {}
            if "name" in body:
                if not isinstance(body["name"], str) or not 1 <= len(body["name"].strip()) <= 80:
                    raise ApiError(400, "BAD_REQUEST", "Name must be 1-80 characters.")
                fields["name"] = body["name"].strip()
            if "clock_seconds" in body:
                if body["clock_seconds"] not in CLOCKS or isinstance(body["clock_seconds"], bool):
                    raise ApiError(400, "BAD_REQUEST", "clock_seconds must be 300, 600 or 3600.")
                fields["clock_seconds"] = body["clock_seconds"]
            if "coding_minutes" in body:
                m = body["coding_minutes"]
                if isinstance(m, bool) or not isinstance(m, int) or not CODING_MIN <= m <= CODING_MAX:
                    raise ApiError(400, "BAD_REQUEST", f"coding_minutes must be {CODING_MIN}-{CODING_MAX}.")
                fields["coding_seconds"] = m * 60
            if not fields:
                raise ApiError(400, "BAD_REQUEST", "Nothing to update.")
            app.store.update_tournament(tid, **fields)
            app.store.audit(user["id"], "tournament.update", tid)
            return 200, app.tournament_json(app.get_tournament(tid))

        def lifecycle(name: str, allowed: tuple[str, ...], new: str, extra: Any = None, pre: Callable | None = None) -> None:
            @r("POST", "/tournaments/{tid}/" + name)
            def handler(ctx: Ctx, tid: str) -> Any:
                user = app.require_organizer(ctx)
                t = app.get_tournament(tid)
                if t["status"] not in allowed:
                    raise ApiError(409, "CONFLICT", f"Cannot {name} while tournament is {t['status']}.")
                if pre:
                    pre(t)
                values = extra(t) if callable(extra) else (extra or {})
                if not app.store.transition(tid, allowed, new, **values):
                    raise ApiError(409, "CONFLICT", "State changed concurrently.")
                app.store.audit(user["id"], f"tournament.{name}", tid)
                return 200, app.tournament_json(app.get_tournament(tid))

        def check_coding(t: dict[str, Any]) -> None:
            if len(app.store.list_entries(t["id"])) < 2:
                raise ApiError(409, "NOT_READY", "At least two entries are required to start the coding phase.")

        def check_lock(t: dict[str, Any]) -> None:
            entries = app.store.list_entries(t["id"])
            if len(entries) < 2:
                raise ApiError(409, "NOT_READY", "At least two entries are required.")
            pending = [e["username"] for e in entries if e["submitted_at"] is None]
            if pending and time.time() < (t["coding_deadline"] or 0):
                raise ApiError(409, "CODING_OPEN", "Coding is still open; entries not submitted yet: " + ", ".join(pending))
            bad = [e["username"] for e in entries if e["status"] != "READY" or e["compile_status"] != "READY"]
            if bad:
                raise ApiError(409, "NOT_READY", "Entries without a submitted bot (remove them first): " + ", ".join(bad))

        def check_start(t: dict[str, Any]) -> None:
            if t["matches_total"] == 0:
                raise ApiError(409, "NO_SCHEDULE", "Generate the schedule first.")

        lifecycle("open-registration", ("DRAFT",), "REGISTRATION_OPEN")
        lifecycle("start-coding", ("REGISTRATION_OPEN",), "CODING",
                  lambda t: {"coding_started_at": int(time.time()), "coding_deadline": int(time.time()) + t["coding_seconds"]}, pre=check_coding)
        lifecycle("lock", ("CODING",), "LOCKED", pre=check_lock)
        lifecycle("start", ("LOCKED",), "RUNNING", lambda t: {"started_at": int(time.time())}, pre=check_start)
        lifecycle("cancel", ("DRAFT", "REGISTRATION_OPEN", "CODING", "LOCKED"), "CANCELLED", lambda t: {"finished_at": int(time.time())})

        @r("POST", "/tournaments/{tid}/schedule")
        def make_schedule(ctx: Ctx, tid: str) -> Any:
            user = app.require_organizer(ctx)
            t = app.get_tournament(tid)
            if t["status"] != "LOCKED":
                raise ApiError(409, "CONFLICT", "Lock the roster before generating the schedule.")
            if t["matches_total"]:
                return 200, {"matches": t["matches_total"]}
            entries = app.store.list_entries(tid)
            rows = iter_round_robin_rows([e["id"] for e in entries])
            match_count = len(entries) * (len(entries) - 1)
            try:
                app.store.create_matches_from_schedule(tid, rows, t["clock_seconds"] * 1000)
            except StoreError as exc:
                raise ApiError(exc.status, exc.code, exc.message) from None
            app.store.audit(user["id"], "tournament.schedule", tid)
            return 201, {"matches": match_count}

        @r("POST", "/tournaments/{tid}/resume")
        def resume(ctx: Ctx, tid: str) -> Any:
            app.require_organizer(ctx)
            app.require_tournament(tid)
            return 200, {"resumed": app.store.resume_paused(tid)}

        @r("GET", "/tournaments/{tid}/schedule")
        def get_schedule(ctx: Ctx, tid: str) -> Any:
            app.require_tournament(tid)
            query = parse_qs(urlsplit(ctx.handler.path).query, keep_blank_values=True)

            def query_int(name: str, default: int, minimum: int, maximum: int) -> int:
                values = query.get(name)
                if values is None:
                    return default
                if (len(values) != 1 or not values[0].isascii() or not values[0].isdigit()
                        or len(values[0]) > len(str(maximum))):
                    raise ApiError(400, "BAD_QUERY", f"Invalid {name} query parameter.")
                value = int(values[0])
                if not minimum <= value <= maximum:
                    raise ApiError(400, "BAD_QUERY", f"Invalid {name} query parameter.")
                return value

            if "recent" in query:
                recent = query_int("recent", 12, 1, 50)
                matches = app.store.list_recent_finished_match_summaries(tid, recent)
                return 200, {"matches": [app.summary(m) for m in matches], "byes": []}

            pagination_values = query.get("pagination", ["offset"])
            if len(pagination_values) != 1 or pagination_values[0] not in ("offset", "keyset"):
                raise ApiError(400, "BAD_QUERY", "Invalid pagination mode.")
            keyset = pagination_values[0] == "keyset"
            cursor_values = query.get("cursor", [])
            direction_values = query.get("direction", [])
            if len(cursor_values) > 1 or len(direction_values) > 1:
                raise ApiError(400, "BAD_QUERY", "Invalid schedule cursor.")
            if direction_values and not cursor_values:
                raise ApiError(400, "BAD_QUERY", "Schedule direction requires a cursor.")
            direction = direction_values[0] if direction_values else "after"
            if direction not in ("after", "before"):
                raise ApiError(400, "BAD_QUERY", "Invalid schedule direction.")
            if cursor_values and not keyset:
                raise ApiError(400, "BAD_QUERY", "Schedule cursor requires keyset pagination.")

            def parse_cursor(value: str) -> tuple[int, int, int, str]:
                fields = value.split(",", 3)
                if (len(value) > 256 or len(fields) != 4
                        or any(
                            not part.isascii() or not part.isdigit() or len(part) > 10
                            for part in fields[:3]
                        )
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", fields[3])):
                    raise ApiError(400, "BAD_QUERY", "Invalid schedule cursor.")
                numbers = tuple(int(part) for part in fields[:3])
                if any(number < 1 for number in numbers):
                    raise ApiError(400, "BAD_QUERY", "Invalid schedule cursor.")
                return numbers[0], numbers[1], numbers[2], fields[3]

            def format_cursor(match: dict[str, Any]) -> str:
                return ",".join(str(match[key]) for key in ("leg_no", "round_no", "pairing_no", "id"))

            entry_count, entry_ids = app.store.schedule_roster(tid)
            paged = "limit" in query or "offset" in query or "cursor" in query or keyset
            if not paged:
                matches = app.store.list_match_summaries(tid)
                return 200, {"matches": [app.summary(m) for m in matches], "byes": byes(entry_ids, matches)}

            limit = query_int("limit", 100, 1, 200)
            offset = query_int("offset", 0, 0, 1_000_000_000)
            total = app.store.count_match_summaries(tid)
            cursor = parse_cursor(cursor_values[0]) if cursor_values else None
            if keyset and cursor is None and offset:
                # Preserve old page URLs once, then issue cursors for fast navigation.
                matches = app.store.list_match_summaries(tid, limit, offset)
                has_more = offset + len(matches) < total
                previous_cursor = format_cursor(matches[0]) if matches else None
                next_cursor = format_cursor(matches[-1]) if has_more and matches else None
            elif keyset:
                matches, has_more = app.store.list_match_summary_page(
                    tid, limit, cursor=cursor, direction=direction,
                )
                has_previous = bool(matches) and (has_more if direction == "before" else cursor is not None)
                has_next = bool(matches) and (cursor is not None if direction == "before" else has_more)
                previous_cursor = format_cursor(matches[0]) if has_previous else None
                next_cursor = format_cursor(matches[-1]) if has_next else None
            else:
                matches = app.store.list_match_summaries(tid, limit, offset)
                has_more = offset + len(matches) < total
                previous_cursor = next_cursor = None
            if entry_count % 2 and total == entry_count * (entry_count - 1):
                bye_rows = [
                    {"leg_no": leg, "round_no": round_no, "entry_id": entry_id}
                    for leg in (1, 2)
                    for round_no, entry_id in enumerate(entry_ids, start=1)
                ] if total else []
            elif entry_count % 2:
                bye_rows = byes(entry_ids, app.store.list_match_summaries(tid))
            else:
                bye_rows = []
            return 200, {
                "matches": [app.summary(m) for m in matches], "byes": bye_rows,
                "total": total, "limit": limit, "offset": offset,
                "has_more": has_more, "previous_cursor": previous_cursor, "next_cursor": next_cursor,
            }

        @r("GET", "/tournaments/{tid}/standings")
        def get_standings(ctx: Ctx, tid: str) -> Any:
            query = ctx.query
            compact_values = query.get("compact", [])
            if len(compact_values) > 1 or (compact_values and compact_values[0] not in ("top3", "top6")):
                raise ApiError(400, "BAD_QUERY", "Invalid standings compact mode.")
            compact = bool(compact_values)
            compact_limit = {"top3": 3, "top6": 6}.get(compact_values[0], 0) if compact else 0
            status = app.get_tournament_status(tid)
            entries = app.store.list_entries(tid)
            matches_count = app.store.count_finished_match_results(tid)
            with closing(app.store.iter_finished_match_results(tid)) as matches:
                rows = standings(entries, matches, matches_in_schedule_order=True,
                                 matches_are_finished=True, matches_count=matches_count,
                                 include_sheet=not compact)
            if compact:
                rows = rows[:compact_limit]
            return 200, {"items": rows, "status": status}

        @r("GET", "/tournaments/{tid}/entries")
        def list_entries(ctx: Ctx, tid: str) -> Any:
            app.require_organizer(ctx)
            app.require_tournament(tid)
            entries = app.store.list_entries(tid, include_submission_status=True)
            return 200, {"items": [app.entry_json(e) for e in entries]}

        @r("GET", "/tournaments/{tid}/entry")
        def my_entry(ctx: Ctx, tid: str) -> Any:
            user = app.require_login(ctx)
            e = app.store.get_user_entry(tid, user["id"], include_submission_status=True)
            if not e or e["status"] == "REMOVED":
                raise ApiError(404, "NOT_FOUND", "No entry.")
            return 200, app.entry_json(e)

        @r("POST", "/tournaments/{tid}/entry")
        def register_entry(ctx: Ctx, tid: str) -> Any:
            user = app.require_login(ctx)
            if user["role"] != "CONTESTANT":
                raise ApiError(403, "FORBIDDEN", "Only contestants can register.")
            if app.get_tournament_status(tid) != "REGISTRATION_OPEN":
                raise ApiError(409, "CONFLICT", "Registration is not open.")
            existing = app.store.get_user_entry(tid, user["id"])
            if existing and existing["status"] != "REMOVED":
                raise ApiError(409, "ENTRY_EXISTS", "This account already has an entry.")
            if existing:
                raise ApiError(409, "ENTRY_REMOVED", "Your entry was removed from this tournament.")
            try:
                e = app.store.create_entry(tid, user["id"])
            except StoreError as exc:
                raise ApiError(exc.status, exc.code, exc.message) from None
            return 201, app.entry_json(e)

        @r("DELETE", "/entries/{eid}")
        def remove_entry(ctx: Ctx, eid: str) -> Any:
            user = app.require_login(ctx)
            e = app.store.get_entry(eid)
            if not e:
                raise ApiError(404, "NOT_FOUND", "Entry not found.")
            if e["user_id"] != user["id"] and user["role"] != "ORGANIZER":
                raise ApiError(403, "FORBIDDEN", "Not your entry.")
            allowed = ("DRAFT", "REGISTRATION_OPEN") if user["role"] != "ORGANIZER" else ("DRAFT", "REGISTRATION_OPEN", "CODING")
            if app.get_tournament_status(e["tournament_id"]) not in allowed:
                raise ApiError(409, "CONFLICT", "Roster is locked.")
            app.store.remove_entry(eid)
            return 204, None

        def my_coding_entry(ctx: Ctx, tid: str) -> tuple[dict[str, Any], dict[str, Any]]:
            user = app.require_login(ctx)
            if user["role"] != "CONTESTANT":
                raise ApiError(403, "FORBIDDEN", "Only contestants write bots.")
            t = app.get_tournament(tid)
            e = app.store.get_user_entry(tid, user["id"])
            if not e or e["status"] == "REMOVED":
                raise ApiError(409, "NO_ENTRY", "Register for the tournament first.")
            return t, e

        def can_edit(t: dict[str, Any], e: dict[str, Any], received_at: float) -> None:
            if t["status"] != "CODING":
                raise ApiError(409, "NOT_CODING", "The coding phase is not open.")
            if e["submitted_at"] is not None:
                raise ApiError(409, "ALREADY_SUBMITTED", "The bot was already submitted and cannot be changed.")
            if app.store.get_pending_submission(e["id"]):
                raise ApiError(409, "SUBMISSION_PENDING", "A final source is already accepted and processing.")
            if received_at >= (t["coding_deadline"] or 0):
                raise ApiError(409, "DEADLINE_PASSED", "The coding deadline has passed.")

        def read_source(ctx: Ctx) -> str:
            src = ctx.json().get("source")
            if not isinstance(src, str):
                raise ApiError(400, "BAD_REQUEST", "Field 'source' (string) is required.")
            try:
                size = len(src.encode("utf-8"))
            except UnicodeError:
                raise ApiError(400, "BAD_REQUEST", "Source must be valid UTF-8.") from None
            if size > MAX_SOURCE:
                raise ApiError(413, "PAYLOAD_TOO_LARGE", "Source exceeds 256 KiB.")
            if "\x00" in src:
                raise ApiError(400, "BAD_REQUEST", "Source contains NUL bytes.")
            app.check_source(src)
            return src

        def read_language(ctx: Ctx, *, query: bool = False) -> str:
            value = ctx.query.get("language", ["cpp17"])[0] if query else ctx.json().get("language", "cpp17")
            try:
                language_profile(value)
            except ValueError as exc:
                raise ApiError(400, "UNSUPPORTED_LANGUAGE", str(exc)) from None
            return value

        @r("GET", "/bot-template")
        def bot_template(ctx: Ctx) -> Any:
            return 200, ("text/plain; charset=utf-8", app.template_source(read_language(ctx, query=True)))

        @r("GET", "/tournaments/{tid}/code")
        def get_code(ctx: Ctx, tid: str) -> Any:
            t, e = my_coding_entry(ctx, tid)
            draft, updated, language = app.store.get_draft(e["id"], with_language=True)
            submitted = e["submitted_at"] is not None
            receipt = app.store.get_pending_submission(e["id"])
            editable = t["status"] == "CODING" and not submitted and not receipt and time.time() < (t["coding_deadline"] or 0)
            return 200, {
                "tournament": app.tournament_json(t), "entry_id": e["id"], "source": draft if draft is not None else app.template_source(language),
                "language": language, "languages": [{"id": key, "label": value.label, "filename": value.filename} for key, value in LANGUAGES.items()],
                "is_template": draft is None, "updated_at": updated, "submitted": submitted, "submitted_at": e["submitted_at"],
                "submit_note": e["submit_note"], "editable": editable, "server_time": int(time.time()),
                "compile_used": e["compile_count"], "compile_limit": COMPILE_LIMIT,
                "pending_submission": ({"receipt_id": receipt["id"], "status": receipt["status"],
                    "received_at": receipt["received_at"], "reason_code": receipt["reason_code"]} if receipt else None),
            }

        @r("PUT", "/tournaments/{tid}/code")
        def save_code(ctx: Ctx, tid: str) -> Any:
            t, e = my_coding_entry(ctx, tid)
            can_edit(t, e, ctx.received_at)
            language = read_language(ctx)
            src = read_source(ctx)
            try:
                stamp = app.store.save_draft(e["id"], src, language)
            except StoreError as exc:
                raise ApiError(exc.status, exc.code, exc.message) from None
            return 200, {"updated_at": stamp, "server_time": int(time.time())}

        @r("POST", "/tournaments/{tid}/code/check")
        def check_code(ctx: Ctx, tid: str) -> Any:
            t, e = my_coding_entry(ctx, tid)
            can_edit(t, e, ctx.received_at)
            language = read_language(ctx)
            src = read_source(ctx)
            uid = ctx.user["id"]  # type: ignore[index]
            if app.store.queued_submissions(limit=1):
                raise ApiError(503, "FINAL_SUBMISSIONS_FIRST", "Final submissions are being processed; retry the check later.")
            if not app.compile_check_limiter.allow("compile-check:" + str(uid), burst=1, per_second=1 / 3):
                raise ApiError(429, "RATE_LIMITED", "Wait a few seconds between compile checks.")
            probe = str(uuid.uuid4())
            try:
                with app.build_slot() as compile_lease:
                    used = app.store.bump_compile(e["id"], COMPILE_LIMIT)
                    try:
                        ok, msg = app.compile_final(probe, src) if language == "cpp17" else app.compile_final(probe, src, language)
                        if ok:
                            app.store.assert_compile_slot(*compile_lease)
                            sha = hashlib.sha256(src.encode("utf-8")).hexdigest()
                            target = artifact_path(app.data_dir, f"check-{e['id']}-{build_key(sha, language)}")
                            if target.exists():
                                runner.SANDBOX.verify_artifact(target, expected_source_hash=sha, expected_language=language)
                            else:
                                runner.SANDBOX.copy_artifact(artifact_path(app.data_dir, probe), target, expected_source_hash=sha, expected_language=language)
                            app.store.set_last_ok(e["id"], build_key(sha, language), compile_lease=compile_lease)
                    except (InfraError, LeaseLost, OSError):
                        app.store.refund_compile(e["id"])
                        raise
            except InfraError as exc:
                raise ApiError(503, "COMPILER_UNAVAILABLE", str(exc)) from None
            finally:
                shutil.rmtree(artifact_path(app.data_dir, probe).parent, ignore_errors=True)
            return 200, {"ok": ok, "message": msg, "compile_used": used, "compile_limit": COMPILE_LIMIT}

        @r("POST", "/tournaments/{tid}/code/submit")
        def submit_code(ctx: Ctx, tid: str) -> Any:
            t, e = my_coding_entry(ctx, tid)
            if ctx.json().get("confirm") is not True:
                raise ApiError(400, "CONFIRM_REQUIRED", "Submission is final; send confirm=true.")
            language = read_language(ctx)
            src = read_source(ctx)
            if not src.strip():
                raise ApiError(400, "BAD_REQUEST", "Source is empty.")
            pending = app.store.get_pending_submission(e["id"])
            if not pending and e["submitted_at"] is None and not app.health_report()["ready"]:
                raise ApiError(503, "NOT_READY", "Submission admission is paused; retry later.")
            receipt = app.store.reserve_submission(e["id"], tid, ctx.user["id"], src, ctx.received_at, language=language,
                                                  runtime_profile=runner.SANDBOX.policy_info())
            return (200 if receipt["status"] == "READY" else 202), {"accepted": True, "receipt_id": receipt["id"], "status": receipt["status"],
                         "received_at": receipt["received_at"], "submitted_at": receipt["received_at"] if receipt["status"] == "READY" else None,
                         "bot_version_id": receipt["bot_id"]}

        @r("GET", "/bots/{bid}/source")
        def bot_source(ctx: Ctx, bid: str) -> Any:
            user = app.require_login(ctx)
            b = app.store.get_bot(bid, with_source=True)
            if not b:
                raise ApiError(404, "NOT_FOUND", "Bot not found.")
            if b["user_id"] != user["id"]:
                raise ApiError(403, "FORBIDDEN", "Only the owner can read the source.")
            return 200, ("text/plain; charset=utf-8", b["source"])

        @r("GET", "/live")
        def live(ctx: Ctx) -> Any:
            tournament_ids = ctx.query.get("tournament_id", [])
            if len(tournament_ids) > 1:
                raise ApiError(400, "BAD_QUERY", "Invalid tournament_id query parameter.")
            tournament_id = tournament_ids[0] if tournament_ids else None
            overviews = app.store.list_matches(
                tid=tournament_id, statuses=("RUNNING",),
                limit=64 if tournament_id else 24, include_state=False,
            )
            misses = [m["id"] for m in overviews if app.cached_view(m, with_moves=False) is None]
            renders = {m["id"]: m for m in app.store.get_match_renders(misses)}
            items = [app.detail(renders.get(m["id"], m), with_moves=False) for m in overviews]
            return 200, {"items": items}

        @r("GET", "/matches/{mid}")
        def match_detail(ctx: Ctx, mid: str) -> Any:
            since = ctx.query.get("since")
            if since and len(since) == 1:
                poll = app.store.get_match_poll(mid)
                if not poll:
                    raise ApiError(404, "NOT_FOUND", "Match not found.")
                state_version = poll.get("state_version")
                if type(state_version) is int and since == [str(state_version)]:
                    clocks, running = app.live_clocks(poll)
                    return 200, {"not_modified": True, "state_version": state_version,
                                 "status": poll["status"], "result": poll["result"],
                                 "result_reason": poll["result_reason"], "clocks_ms": clocks,
                                 "running_side": running}
            m = app.store.get_match_overview(mid)
            if not m:
                raise ApiError(404, "NOT_FOUND", "Match not found.")
            out = app.detail(m)
            out["viewer_is_player"] = bool(ctx.user and ctx.user["username"] in (m["white_name"], m["black_name"]))
            return 200, out

        @r("GET", "/users/{name}")
        def profile(ctx: Ctx, name: str) -> Any:
            u = app.store.get_user(name)
            if not u:
                raise ApiError(404, "NOT_FOUND", "User not found.")
            games = []
            totals = app.store.user_match_totals(u["id"])
            for m in app.store.user_matches(u["id"], include_state=False):
                white = m["white_name"] == u["username"]
                outcome = None
                if m["status"] == "FINISHED":
                    outcome = "draw" if m["result"] == "DRAW" else "win" if (m["result"] == "WHITE_WIN") == white else "loss"
                games.append({"id": m["id"], "tournament_name": m["tournament_name"], "opponent": m["black_name"] if white else m["white_name"],
                              "color": "white" if white else "black", "status": m["status"], "outcome": outcome, "reason": m["result_reason"]})
            entries = []
            with closing(app.store.iter_user_standings_inputs(
                    u["id"], finished_matches_only=True, stream_matches=True)) as inputs:
                for tournament in inputs:
                    rows = standings(tournament["entries"], tournament["matches"],
                                     matches_are_finished=True,
                                     matches_count=tournament["matches_count"], include_sheet=False)
                    mine = next((row for row in rows if row["entry_id"] == tournament["entry_id"]), None)
                    entries.append({"tournament_id": tournament["tournament_id"], "tournament_name": tournament["tournament_name"],
                                    "status": tournament["status"], "rank": mine["rank"] if mine else None,
                                    "points": mine["points"] if mine else 0, "compile_status": tournament["compile_status"]})
            return 200, {"username": u["username"], "role": u["role"], "created_at": u["created_at"], "totals": totals, "games": games, "entries": entries}


class Ctx:
    def __init__(self, handler: "Handler", user: dict[str, Any] | None, token: str | None) -> None:
        self.handler, self.user, self.session_token = handler, user, token
        self.set_session: str | None = None
        self.clear_session = False
        self._body: bytes | None = None
        self._json: dict[str, Any] | None = None
        self.received_at = time.time()
        self.query = parse_qs(urlsplit(handler.path).query)

    @property
    def client_ip(self) -> str:
        return self.handler.request_ip

    def raw(self) -> bytes:
        if self._body is None:
            lengths = self.handler.headers.get_all("Content-Length", [])
            if self.handler.headers.get_all("Transfer-Encoding", []) or len(lengths) > 1:
                raise ApiError(400, "BAD_REQUEST", "Ambiguous request framing.")
            try:
                value = lengths[0] if lengths else "0"
                if not value.isascii() or not value.isdigit():
                    raise ValueError
                length = int(value)
            except ValueError:
                raise ApiError(400, "BAD_REQUEST", "Bad Content-Length.") from None
            if length > self.handler.app.policy.max_body:
                raise ApiError(413, "PAYLOAD_TOO_LARGE", "Request body too large.")
            if self.handler.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ApiError(415, "UNSUPPORTED_ENCODING", "Encoded request bodies are not supported.")
            if length and self.handler.headers.get_content_type() != "application/json":
                raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json.")
            self._body = self.handler.rfile.read(length) if length else b""
            if len(self._body) != length:
                raise ApiError(400, "BAD_REQUEST", "Incomplete request body.")
            self.received_at = time.time()
            self.handler.body_received()
        return self._body

    def json(self) -> dict[str, Any]:
        if self._json is not None:
            return self._json
        raw = self.raw()
        if not raw:
            self._json = {}
            return self._json
        depth = 0
        position = 0
        # Match strings from their known start; possessive repeats bound malformed input work.
        while match := _JSON_MARKERS.search(raw, position):
            index = match.start()
            marker = raw[index]
            position = index + 1
            if marker == 34:
                position = _json_string_end(raw, index)
            elif marker == 92:
                raise ApiError(400, "BAD_REQUEST", "Body must be valid JSON.")
            elif marker in (123, 91):
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ApiError(400, "BAD_REQUEST", "Body JSON nesting exceeds 64 levels.")
            elif marker in (125, 93):
                depth -= 1
        try:
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON field.")
                    result[key] = value
                return result

            def invalid_constant(value):
                raise ValueError("Non-finite JSON number.")

            def finite_float(value):
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("Non-finite JSON number.")
                return number

            data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=invalid_constant, parse_float=finite_float)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ApiError(400, "BAD_REQUEST", "Body must be valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "BAD_REQUEST", "Body must be a JSON object.")
        self._json = data
        return self._json


class _DeadlineScheduler:
    """Watch all request sockets with one bounded timer thread."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active: dict[ConnectionDeadline, tuple[float, int]] = {}
        self._heap: list[tuple[float, int, ConnectionDeadline]] = []
        self._serial = 0
        self._clients = 0
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="request-deadlines")
        self._thread.start()

    def arm(self, deadline: "ConnectionDeadline", timeout: float) -> None:
        with self._condition:
            if deadline._closed or deadline._fired:
                return
            due = time.monotonic() + timeout
            self._serial += 1
            deadline._deadline = due
            deadline._callback_done.clear()
            self._active[deadline] = (due, self._serial)
            heapq.heappush(self._heap, (due, self._serial, deadline))
            self._compact()
            self._condition.notify()

    def cancel(self, deadline: "ConnectionDeadline", *, close: bool = False) -> bool:
        with self._condition:
            was_active = self._active.pop(deadline, None) is not None
            deadline._deadline = None
            if was_active:
                deadline._callback_done.set()
            release = close and not deadline._closed
            if close:
                deadline._closed = True
            self._compact()
            self._condition.notify()
            return release

    def stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=1)

    def _compact(self) -> None:
        if len(self._heap) > max(128, 2 * len(self._active)):
            self._heap = [(due, serial, deadline)
                          for deadline, (due, serial) in self._active.items()]
            heapq.heapify(self._heap)

    def _run(self) -> None:
        while True:
            with self._condition:
                while True:
                    if self._stopping:
                        return
                    if not self._heap:
                        self._condition.wait()
                        continue
                    due, serial, deadline = self._heap[0]
                    active = self._active.get(deadline)
                    if active is None or active[1] != serial:
                        heapq.heappop(self._heap)
                        continue
                    remaining = due - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                    heapq.heappop(self._heap)
                    del self._active[deadline]
                    deadline._deadline = None
                    deadline._fired = True
                    connection = deadline._connection
                    break
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                deadline._callback_done.set()


_DEADLINE_SCHEDULER: _DeadlineScheduler | None = None
_DEADLINE_SCHEDULER_LOCK = threading.Lock()


def _deadline_scheduler() -> _DeadlineScheduler:
    global _DEADLINE_SCHEDULER
    with _DEADLINE_SCHEDULER_LOCK:
        if _DEADLINE_SCHEDULER is None:
            _DEADLINE_SCHEDULER = _DeadlineScheduler()
        _DEADLINE_SCHEDULER._clients += 1
        return _DEADLINE_SCHEDULER


def _release_deadline_scheduler(scheduler: _DeadlineScheduler) -> None:
    global _DEADLINE_SCHEDULER
    with _DEADLINE_SCHEDULER_LOCK:
        if scheduler is not _DEADLINE_SCHEDULER:
            return
        scheduler._clients -= 1
        if scheduler._clients == 0:
            _DEADLINE_SCHEDULER = None
            scheduler.stop()


class ConnectionDeadline:
    """Enforce absolute request deadlines through the shared timer thread."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._deadline: float | None = None
        self._closed = False
        self._fired = False
        self._callback_done = threading.Event()
        self._callback_done.set()
        self._scheduler = _deadline_scheduler()
        self._thread = self._scheduler._thread

    def arm(self, timeout: float) -> None:
        self._scheduler.arm(self, timeout)

    def cancel(self) -> None:
        self._scheduler.cancel(self)

    def close(self) -> None:
        release = self._scheduler.cancel(self, close=True)
        self._callback_done.wait(timeout=1)
        if release:
            _release_deadline_scheduler(self._scheduler)


class Handler(BaseHTTPRequestHandler):
    server_version = "OTTArena/1.0"
    protocol_version = "HTTP/1.1"
    app: App
    timeout = 15
    request_deadline_seconds = 20

    def setup(self) -> None:
        super().setup()
        self.app.store.enable_thread_connection_reuse()
        self.request_deadline = ConnectionDeadline(self.connection)

    def handle_one_request(self) -> None:
        # Idle timeout alone does not stop a client sending one byte at a time.
        self.request_deadline.arm(self.request_deadline_seconds)
        try:
            super().handle_one_request()
        except OSError:
            self.close_connection = True
        finally:
            self.request_deadline.cancel()

    def body_received(self) -> None:
        self.request_deadline.cancel()

    def finish(self) -> None:
        deadline = getattr(self, "request_deadline", None)
        try:
            if deadline is not None:
                deadline.close()
            super().finish()
        finally:
            self.app.store.close_thread_connection()

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def _security_headers(self, *, cache_control: str = "no-store") -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Request-ID", getattr(self, "request_id", uuid.uuid4().hex))

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    do_POST = do_PUT = do_PATCH = do_DELETE = lambda self: self._dispatch(self.command)  # type: ignore[assignment]

    def _session(self) -> tuple[dict[str, Any] | None, str | None]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None, None
        try:
            cookie = SimpleCookie(raw)
        except Exception:  # noqa: BLE001
            return None, None
        morsel = cookie.get("ott_session")
        if not morsel:
            return None, None
        return self.app.store.get_session(morsel.value), morsel.value

    def _send(self, status: int, payload: Any, ctx: Ctx | None = None) -> None:
        body = b""
        ctype = "application/json; charset=utf-8"
        if isinstance(payload, tuple):
            ctype, text = payload
            body = text.encode("utf-8")
        elif payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        if self.close_connection:
            self.send_header("Connection", "close")
        if status in (429, 503):
            self.send_header("Retry-After", "5")
        if ctx and ctx.set_session:
            flags = "HttpOnly; SameSite=Lax; Path=/; Max-Age=43200" + ("; Secure" if self.app.secure_cookie else "")
            self.send_header("Set-Cookie", f"ott_session={ctx.set_session}; {flags}")
        if ctx and ctx.clear_session:
            flags = "; Secure" if self.app.secure_cookie else ""
            self.send_header("Set-Cookie", "ott_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0" + flags)
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass  # client went away

    def _dispatch(self, method: str) -> None:
        self.request_id = uuid.uuid4().hex
        self.request_ip = self.client_address[0]
        ctx = None
        intake_key = None
        try:
            self.app.policy.validate_request(self.headers, method, self.path)
            proxied = self.app.policy.authenticate(self.headers, method)
            self.request_ip = self.app.policy.client_ip(self.headers, self.client_address[0], proxied)
            if len(self.path) > 8192:
                raise ApiError(414, "URI_TOO_LONG", "Request target is too long.")
            if not self.app.rate_limiter.allow("request:" + self.request_ip, burst=240, per_second=60):
                raise ApiError(429, "RATE_LIMITED", "Too many requests.")
            path = unquote(urlsplit(self.path).path)
            if method == "GET":
                self.body_received()
            if path == "/healthz" and method == "GET":
                self._send(200, {"alive": True})
                return
            if path == "/readyz" and method == "GET":
                report = self.app.health_report()
                # Public checks expose no runtime metadata.
                self._send(200 if report["ready"] else 503, {"ready": report["ready"], "checks": report["checks"]})
                return
            if not path.startswith("/api/v1/"):
                if method != "GET":
                    raise ApiError(405, "METHOD_NOT_ALLOWED", "Method not allowed.")
                self._static(path)
                return
            sub = path[len("/api/v1"):]
            if sub in ("/auth/login", "/auth/register"):
                if not self.app.auth_limiter.allow("auth:" + self.request_ip, burst=30, per_second=1):
                    raise ApiError(429, "RATE_LIMITED", "Too many authentication requests.")
            if method != "GET" and maintenance_enabled(self.app.data_dir) and sub not in (
                    "/ops/maintenance", "/auth/login", "/auth/logout"):
                raise ApiError(503, "MAINTENANCE", "Maintenance is active; retry later.")
            user, token = self._session()
            if user and not self.app.account_limiter.allow("account:" + str(user["id"]), burst=180, per_second=30):
                raise ApiError(429, "RATE_LIMITED", "Too many requests for this account.")
            intake_match = re.fullmatch(r"/tournaments/([0-9a-f-]{36})/code(?:/submit)?", sub)
            final_or_draft = (method == "POST" and sub.endswith("/code/submit")) or (method == "PUT" and sub.endswith("/code"))
            if final_or_draft and user and intake_match:
                intake_key = (intake_match.group(1), user["id"])
                with self.app.intake_lock:
                    self.app.intakes[intake_key] = self.app.intakes.get(intake_key, 0) + 1
            ctx = Ctx(self, user, token)
            if method != "GET":
                ctx.json()
            if sub == "/stream" and method == "GET":
                if not self.app.policy.allow_stream:
                    raise ApiError(404, "STREAM_DISABLED", "Use polling for this deployment.")
                self._stream()
                return
            prefix = sub.lstrip("/").split("/", 1)[0]
            prefix_routes = self.app.routes_by_prefix.get(prefix, {})
            for _, regex, fn, kind in prefix_routes.get(method, ()):
                hit = regex.match(sub)
                if not hit:
                    continue
                if kind == "mut" and sub not in ("/auth/login", "/auth/register"):
                    if not user or not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode("utf-8"), user["csrf_token"].encode("utf-8")):
                        if not user:
                            raise ApiError(401, "AUTH_REQUIRED", "Log in first.")
                        raise ApiError(403, "CSRF", "Missing or invalid CSRF token.")
                status, payload = fn(ctx, **hit.groupdict())
                self._send(status, payload, ctx)
                return
            matched = any(
                regex.match(sub)
                for route_method, routes in prefix_routes.items() if route_method != method
                for _, regex, _, _ in routes
            )
            raise ApiError(405 if matched else 404, "METHOD_NOT_ALLOWED" if matched else "NOT_FOUND", "No such endpoint.")
        except (ApiError, RequestRejected) as exc:
            self.close_connection = True
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}}, ctx)
        except StoreError as exc:
            self.close_connection = True
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}}, ctx)
        except sqlite3.Error:
            self.close_connection = True
            self._send(503, {"error": {"code": "DATABASE_UNAVAILABLE", "message": "Storage is unavailable; retry later."}}, ctx)
        except (socket.timeout, TimeoutError):
            self.close_connection = True
            self._send(408, {"error": {"code": "REQUEST_TIMEOUT", "message": "Request timed out."}}, ctx)
        except Exception:  # noqa: BLE001
            self.close_connection = True
            LOG.exception("unhandled error for %s %s", method, path)
            self._send(500, {"error": {"code": "INTERNAL", "message": "Internal server error."}}, ctx)
        finally:
            if intake_key:
                with self.app.intake_lock:
                    remaining = self.app.intakes[intake_key] - 1
                    if remaining:
                        self.app.intakes[intake_key] = remaining
                    else:
                        self.app.intakes.pop(intake_key, None)

    def _stream(self) -> None:
        """Server-Sent Events: push change notifications so every client updates immediately."""
        sub = HUB.subscribe()
        if sub is None:
            self._send(503, {"error": {"code": "BUSY", "message": "Too many live connections."}})
            return
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.write(b'event: hello\ndata: {"type":"hello"}\n\n')
            self.wfile.flush()
            while not sub.closed and not self.app.stop_event.is_set():
                batch = sub.get_batch(15.0)
                if batch is None:
                    self.wfile.write(b": ping\n\n")  # keep-alive through idle proxies / NAT
                else:
                    for event in batch:
                        self.wfile.write(b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (OSError, ValueError):
            pass  # client disconnected
        finally:
            HUB.unsubscribe(sub)

    def _static(self, path: str) -> None:
        root = self.app.static_dir.resolve()
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        if root not in target.parents and target != root or not target.is_file():
            if "." not in Path(rel).name:
                target = root / "index.html"
            else:
                self._send(404, {"error": {"code": "NOT_FOUND", "message": "Not found."}})
                return
        data, etag = self.app.static_cache.read(target)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        conditions = self.headers.get("If-None-Match", "")
        if conditions.strip() == "*" or any(tag.strip().removeprefix("W/") == etag for tag in conditions.split(",")):
            self.send_response(304)
            self.send_header("ETag", etag)
            self._security_headers(cache_control="no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self._security_headers(cache_control="no-cache")
        self.end_headers()
        self.wfile.write(data)


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 32

    def __init__(self, address, handler, app: App):
        self.app = app
        self.slots = threading.BoundedSemaphore(app.policy.connection_limit)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\nRetry-After: 5\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def server_close(self):
        self.app.stop()
        super().server_close()


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("Private API must bind IPv4 loopback; use the authenticated gateway.")
    handler = type("ContestHandler", (Handler,), {"app": app})
    server = BoundedHTTPServer((host, port), handler, app)
    app.start()
    return server
