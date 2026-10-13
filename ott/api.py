"""HTTP gateway: JSON API under /api/v1 plus static web client."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import shutil
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from ott import engine
from ott.runner import LIVE, LIVE_LOCK, InfraError, artifact_path, compile_source
from ott.store import Store, StoreError
from ott.tournament import Dispatcher, byes, round_robin, standings

LOG = logging.getLogger("ott.api")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
MAX_BODY = 400 * 1024
MAX_SOURCE = 256 * 1024
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "samples" / "bot_template.cpp"
CODING_MIN, CODING_MAX = 1, 240
COMPILE_LIMIT = 10
CLOCKS = (300, 600, 3600)
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def make_app(store: Store, data_dir: Path, static_dir: Path, secure_cookie: bool = False, workers: int = 4,
             open_registration: bool = True) -> "App":
    app = App(store, data_dir, static_dir, secure_cookie, workers)
    app.open_registration = open_registration
    return app


class App:
    def __init__(self, store: Store, data_dir: Path, static_dir: Path, secure_cookie: bool, workers: int) -> None:
        self.store, self.data_dir, self.static_dir, self.secure_cookie = store, data_dir, static_dir, secure_cookie
        self.dispatcher = Dispatcher(store, data_dir, workers)
        self.compile_slots = threading.Semaphore(2)
        self.check_times: dict[int, float] = {}
        self.open_registration = True
        self.fails: dict[str, list[float]] = {}
        self.fail_lock = threading.Lock()
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., Any], str]] = []
        self._register_routes()

    def start(self) -> None:
        self.dispatcher.start()
        threading.Thread(target=self._coding_watcher, daemon=True, name="coding-watcher").start()

    def template_source(self) -> str:
        try:
            return TEMPLATE_PATH.read_text(encoding="utf-8")
        except OSError:
            return "// OTT bot template missing\nint main() { return 0; }\n"

    def compile_final(self, bot_id: str, source: str) -> tuple[bool, str | None]:
        with self.compile_slots:
            ok, msg = compile_source(artifact_path(self.data_dir, bot_id), source)
        if not ok:
            shutil.rmtree(artifact_path(self.data_dir, bot_id).parent, ignore_errors=True)
        return ok, msg

    def check_dir(self, entry_id: str) -> Path:
        return artifact_path(self.data_dir, f"check-{entry_id}").parent

    def build_final(self, entry_id: str, bot_id: str, source: str, count: bool) -> tuple[bool, str | None]:
        """Produce the final binary: reuse the last successful check build of identical source (free),
        otherwise compile (consuming one of the COMPILE_LIMIT attempts when `count`)."""
        sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
        entry = self.store.get_entry(entry_id)
        cached = artifact_path(self.data_dir, f"check-{entry_id}")
        if entry and entry["last_ok_sha"] == sha and cached.exists():
            dest = artifact_path(self.data_dir, bot_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, dest)
            return True, None
        if count:
            self.store.bump_compile(entry_id, COMPILE_LIMIT)
        return self.compile_final(bot_id, source)

    def _coding_watcher(self) -> None:
        """Auto-submit the saved draft of every entry that missed the coding deadline."""
        while True:
            try:
                for row in self.store.expired_coding():
                    source = (row["draft_source"] or "").strip()
                    if not source:
                        self.store.set_submit_note(row["eid"], "Hết giờ: chưa có mã nguồn để nộp.")
                        continue
                    bot_id = str(uuid.uuid4())
                    ok, msg = self.build_final(row["eid"], bot_id, row["draft_source"], count=False)
                    if not ok:
                        first = (msg or "lỗi biên dịch").strip().splitlines()[0][:200]
                        self.store.set_submit_note(row["eid"], f"Hết giờ: bản nháp không biên dịch được ({first})")
                    elif self.store.finalize_entry(row["eid"], row["tid"], row["user_id"], bot_id, row["draft_source"]):
                        self.store.audit(None, "code.auto_submit", row["eid"])
                    else:
                        shutil.rmtree(artifact_path(self.data_dir, bot_id).parent, ignore_errors=True)
            except InfraError as exc:
                LOG.warning("coding watcher: %s", exc)
            except Exception:  # noqa: BLE001
                LOG.exception("coding watcher failed")
            time.sleep(1.0)

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

    def detail(self, m: dict[str, Any], with_moves: bool = True) -> dict[str, Any]:
        out = self.summary(m)
        state = json.loads(m["state_json"]) if m["state_json"] else None
        clocks, running = self.live_clocks(m)
        out["clocks_ms"] = clocks
        out["running_side"] = running
        out["clock_seconds"] = m["clock_seconds"]
        out["placed"] = engine.placed_counts(state) if state else {"white": 0, "black": 0}
        revealed = bool(state and state["phase"] == "play")
        out["revealed"] = revealed
        out["board"] = engine.public_board(state) if state else None
        out["placements"] = [h["action"] | {"side": h["side"]} for h in state["history"] if h["phase"] == "placement"] if revealed else None
        if with_moves:
            out["moves"] = state["moves_log"] if state else []
        out["move_count"] = state["move_count"] if state else 0
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

    # ---------- routes ----------
    def route(self, method: str, pattern: str, mutating: bool | None = None) -> Callable:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def deco(fn: Callable) -> Callable:
            self.routes.append((method, regex, fn, "mut" if (mutating if mutating is not None else method != "GET") else "read"))
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
            if not isinstance(username, str) or not USERNAME_RE.match(username):
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
            if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
                raise ApiError(400, "BAD_REQUEST", "username and password are required.")
            key = f"{ctx.client_ip}|{username.lower()}"
            with app.fail_lock:
                recent = [t for t in app.fails.get(key, []) if time.time() - t < 300]
                app.fails[key] = recent
                if len(recent) >= 10:
                    raise ApiError(429, "RATE_LIMITED", "Too many attempts. Try again later.")
            user = app.store.verify_login(username, password)
            if not user:
                with app.fail_lock:
                    app.fails.setdefault(key, []).append(time.time())
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
            ts = app.store.list_tournaments()
            return 200, {
                "users": app.store.user_count(),
                "tournaments": len(ts),
                "running_tournaments": sum(t["status"] == "RUNNING" for t in ts),
                "running_matches": sum(t["matches_running"] for t in ts),
            }

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
            t = app.get_tournament(tid)
            if t["status"] != "DRAFT":
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
            rows = round_robin([e["id"] for e in entries])
            try:
                app.store.create_matches(tid, rows, t["clock_seconds"] * 1000)
            except StoreError as exc:
                raise ApiError(exc.status, exc.code, exc.message) from None
            app.store.audit(user["id"], "tournament.schedule", tid)
            return 201, {"matches": len(rows)}

        @r("POST", "/tournaments/{tid}/resume")
        def resume(ctx: Ctx, tid: str) -> Any:
            app.require_organizer(ctx)
            app.get_tournament(tid)
            return 200, {"resumed": app.store.resume_paused(tid)}

        @r("GET", "/tournaments/{tid}/schedule")
        def get_schedule(ctx: Ctx, tid: str) -> Any:
            app.get_tournament(tid)
            matches = app.store.list_matches(tid)
            entries = app.store.list_entries(tid)
            return 200, {"matches": [app.summary(m) for m in matches], "byes": byes([e["id"] for e in entries], matches)}

        @r("GET", "/tournaments/{tid}/standings")
        def get_standings(ctx: Ctx, tid: str) -> Any:
            t = app.get_tournament(tid)
            rows = standings(app.store.list_entries(tid), app.store.list_matches(tid))
            return 200, {"items": rows, "status": t["status"]}

        @r("GET", "/tournaments/{tid}/entries")
        def list_entries(ctx: Ctx, tid: str) -> Any:
            app.require_organizer(ctx)
            app.get_tournament(tid)
            return 200, {"items": [app.entry_json(e) for e in app.store.list_entries(tid)]}

        @r("GET", "/tournaments/{tid}/entry")
        def my_entry(ctx: Ctx, tid: str) -> Any:
            user = app.require_login(ctx)
            e = app.store.get_user_entry(tid, user["id"])
            if not e or e["status"] == "REMOVED":
                raise ApiError(404, "NOT_FOUND", "No entry.")
            return 200, app.entry_json(e)

        @r("POST", "/tournaments/{tid}/entry")
        def register_entry(ctx: Ctx, tid: str) -> Any:
            user = app.require_login(ctx)
            if user["role"] != "CONTESTANT":
                raise ApiError(403, "FORBIDDEN", "Only contestants can register.")
            t = app.get_tournament(tid)
            if t["status"] != "REGISTRATION_OPEN":
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
            if app.get_tournament(e["tournament_id"])["status"] not in allowed:
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

        def can_edit(t: dict[str, Any], e: dict[str, Any]) -> None:
            if t["status"] != "CODING":
                raise ApiError(409, "NOT_CODING", "The coding phase is not open.")
            if e["submitted_at"] is not None:
                raise ApiError(409, "ALREADY_SUBMITTED", "The bot was already submitted and cannot be changed.")
            if time.time() >= (t["coding_deadline"] or 0):
                raise ApiError(409, "DEADLINE_PASSED", "The coding deadline has passed.")

        def read_source(ctx: Ctx) -> str:
            src = ctx.json().get("source")
            if not isinstance(src, str):
                raise ApiError(400, "BAD_REQUEST", "Field 'source' (string) is required.")
            if len(src.encode("utf-8")) > MAX_SOURCE:
                raise ApiError(413, "PAYLOAD_TOO_LARGE", "Source exceeds 256 KiB.")
            if "\x00" in src:
                raise ApiError(400, "BAD_REQUEST", "Source contains NUL bytes.")
            return src

        @r("GET", "/bot-template")
        def bot_template(ctx: Ctx) -> Any:
            return 200, ("text/plain; charset=utf-8", app.template_source())

        @r("GET", "/tournaments/{tid}/code")
        def get_code(ctx: Ctx, tid: str) -> Any:
            t, e = my_coding_entry(ctx, tid)
            draft, updated = app.store.get_draft(e["id"])
            submitted = e["submitted_at"] is not None
            editable = t["status"] == "CODING" and not submitted and time.time() < (t["coding_deadline"] or 0)
            return 200, {
                "tournament": app.tournament_json(t), "entry_id": e["id"], "source": draft if draft is not None else app.template_source(),
                "is_template": draft is None, "updated_at": updated, "submitted": submitted, "submitted_at": e["submitted_at"],
                "submit_note": e["submit_note"], "editable": editable, "server_time": int(time.time()),
                "compile_used": e["compile_count"], "compile_limit": COMPILE_LIMIT,
            }

        @r("PUT", "/tournaments/{tid}/code")
        def save_code(ctx: Ctx, tid: str) -> Any:
            t, e = my_coding_entry(ctx, tid)
            can_edit(t, e)
            src = read_source(ctx)
            try:
                stamp = app.store.save_draft(e["id"], src)
            except StoreError as exc:
                raise ApiError(exc.status, exc.code, exc.message) from None
            return 200, {"updated_at": stamp, "server_time": int(time.time())}

        @r("POST", "/tournaments/{tid}/code/check")
        def check_code(ctx: Ctx, tid: str) -> Any:
            t, e = my_coding_entry(ctx, tid)
            can_edit(t, e)
            src = read_source(ctx)
            uid = ctx.user["id"]  # type: ignore[index]
            if time.time() - app.check_times.get(uid, 0.0) < 3:
                raise ApiError(429, "RATE_LIMITED", "Wait a few seconds between compile checks.")
            try:
                used = app.store.bump_compile(e["id"], COMPILE_LIMIT)
            except StoreError as exc:
                raise ApiError(exc.status, exc.code, exc.message) from None
            app.check_times[uid] = time.time()
            probe = str(uuid.uuid4())
            try:
                ok, msg = app.compile_final(probe, src)
                if ok:
                    keep = app.check_dir(e["id"])
                    shutil.rmtree(keep, ignore_errors=True)
                    shutil.copytree(artifact_path(app.data_dir, probe).parent, keep)
                    app.store.set_last_ok(e["id"], hashlib.sha256(src.encode("utf-8")).hexdigest())
            except InfraError as exc:
                raise ApiError(503, "COMPILER_UNAVAILABLE", str(exc)) from None
            finally:
                shutil.rmtree(artifact_path(app.data_dir, probe).parent, ignore_errors=True)
            return 200, {"ok": ok, "message": msg, "compile_used": used, "compile_limit": COMPILE_LIMIT}

        @r("POST", "/tournaments/{tid}/code/submit")
        def submit_code(ctx: Ctx, tid: str) -> Any:
            t, e = my_coding_entry(ctx, tid)
            can_edit(t, e)
            if ctx.json().get("confirm") is not True:
                raise ApiError(400, "CONFIRM_REQUIRED", "Submission is final; send confirm=true.")
            src = read_source(ctx)
            if not src.strip():
                raise ApiError(400, "BAD_REQUEST", "Source is empty.")
            bot_id = str(uuid.uuid4())
            try:
                ok, msg = app.build_final(e["id"], bot_id, src, count=True)
            except InfraError as exc:
                raise ApiError(503, "COMPILER_UNAVAILABLE", str(exc)) from None
            except StoreError as exc:
                raise ApiError(exc.status, exc.code, exc.message + " Only the last successfully compiled source can still be submitted.") from None
            if not ok:
                app.store.save_draft(e["id"], src)
                raise ApiError(422, "COMPILE_ERROR", msg or "Compile failed.")
            uid = ctx.user["id"]  # type: ignore[index]
            if not app.store.finalize_entry(e["id"], tid, uid, bot_id, src):
                shutil.rmtree(artifact_path(app.data_dir, bot_id).parent, ignore_errors=True)
                raise ApiError(409, "ALREADY_SUBMITTED", "The bot was already submitted and cannot be changed.")
            shutil.rmtree(app.check_dir(e["id"]), ignore_errors=True)
            app.store.audit(uid, "code.submit", e["id"])
            return 201, {"submitted_at": int(time.time()), "bot_version_id": bot_id}

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
            ms = app.store.list_matches(statuses=("RUNNING",), limit=24)
            return 200, {"items": [app.detail(m, with_moves=False) | {"last_move": _last_move(m)} for m in ms]}

        @r("GET", "/matches/{mid}")
        def match_detail(ctx: Ctx, mid: str) -> Any:
            m = app.store.get_match(mid)
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
            totals = {"wins": 0, "draws": 0, "losses": 0, "points": 0}
            for m in app.store.user_matches(u["id"]):
                white = m["white_name"] == u["username"]
                outcome = None
                if m["status"] == "FINISHED":
                    outcome = "draw" if m["result"] == "DRAW" else "win" if (m["result"] == "WHITE_WIN") == white else "loss"
                    key = {"win": ("wins", 3), "draw": ("draws", 1), "loss": ("losses", 0)}[outcome]
                    totals[key[0]] += 1
                    totals["points"] += key[1]
                games.append({"id": m["id"], "tournament_name": m["tournament_name"], "opponent": m["black_name"] if white else m["white_name"],
                              "color": "white" if white else "black", "status": m["status"], "outcome": outcome, "reason": m["result_reason"]})
            entries = []
            for e in app.store.user_entries(u["id"]):
                t = app.store.get_tournament(e["tournament_id"])
                rows = standings(app.store.list_entries(t["id"]), app.store.list_matches(t["id"]))
                mine = next((r for r in rows if r["entry_id"] == e["id"]), None)
                entries.append({"tournament_id": t["id"], "tournament_name": t["name"], "status": t["status"],
                                "rank": mine["rank"] if mine else None, "points": mine["points"] if mine else 0, "compile_status": e["compile_status"]})
            return 200, {"username": u["username"], "role": u["role"], "created_at": u["created_at"], "totals": totals, "games": games, "entries": entries}

        def _last_move(m: dict[str, Any]) -> dict[str, str] | None:
            if not m["state_json"]:
                return None
            log = json.loads(m["state_json"])["moves_log"]
            return {"from": log[-1]["from"], "to": log[-1]["to"]} if log else None


class Ctx:
    def __init__(self, handler: "Handler", user: dict[str, Any] | None, token: str | None) -> None:
        self.handler, self.user, self.session_token = handler, user, token
        self.set_session: str | None = None
        self.clear_session = False
        self._body: bytes | None = None
        self.query = parse_qs(urlsplit(handler.path).query)

    @property
    def client_ip(self) -> str:
        return self.handler.client_address[0]

    def raw(self) -> bytes:
        if self._body is None:
            try:
                length = int(self.handler.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "BAD_REQUEST", "Bad Content-Length.") from None
            if length > MAX_BODY:
                raise ApiError(413, "PAYLOAD_TOO_LARGE", "Request body too large.")
            self._body = self.handler.rfile.read(length) if length else b""
        return self._body

    def json(self) -> dict[str, Any]:
        raw = self.raw()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "BAD_REQUEST", "Body must be valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "BAD_REQUEST", "Body must be a JSON object.")
        return data


class Handler(BaseHTTPRequestHandler):
    server_version = "OTTArena/1.0"
    app: App

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")

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
        if ctx and ctx.set_session:
            flags = "HttpOnly; SameSite=Lax; Path=/; Max-Age=43200" + ("; Secure" if self.app.secure_cookie else "")
            self.send_header("Set-Cookie", f"ott_session={ctx.set_session}; {flags}")
        if ctx and ctx.clear_session:
            self.send_header("Set-Cookie", "ott_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass  # client went away

    def _dispatch(self, method: str) -> None:
        path = unquote(urlsplit(self.path).path)
        if not path.startswith("/api/v1/"):
            if method != "GET":
                self._send(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "Method not allowed."}})
                return
            self._static(path)
            return
        sub = path[len("/api/v1"):]
        user, token = self._session()
        ctx = Ctx(self, user, token)
        try:
            matched = False
            for m, regex, fn, kind in self.app.routes:
                hit = regex.match(sub)
                if not hit:
                    continue
                matched = True
                if m != method:
                    continue
                if kind == "mut" and sub not in ("/auth/login", "/auth/register"):
                    origin = self.headers.get("Origin")
                    if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                        raise ApiError(403, "FORBIDDEN", "Cross-origin request rejected.")
                    if not user or self.headers.get("X-CSRF-Token") != user["csrf_token"]:
                        if not user:
                            raise ApiError(401, "AUTH_REQUIRED", "Log in first.")
                        raise ApiError(403, "CSRF", "Missing or invalid CSRF token.")
                status, payload = fn(ctx, **hit.groupdict())
                self._send(status, payload, ctx)
                return
            raise ApiError(405 if matched else 404, "METHOD_NOT_ALLOWED" if matched else "NOT_FOUND", "No such endpoint.")
        except ApiError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}}, ctx)
        except StoreError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}}, ctx)
        except Exception:  # noqa: BLE001
            LOG.exception("unhandled error for %s %s", method, path)
            self._send(500, {"error": {"code": "INTERNAL", "message": "Internal server error."}}, ctx)

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
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    Handler.app = app
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    app.start()
    return server
