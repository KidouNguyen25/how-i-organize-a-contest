"""Interactive match runner. Compiling and executing bots goes through `ott.sandbox`:
host processes (dev) or Docker containers (network-less, read-only, resource-limited)."""

from __future__ import annotations

import json
import hashlib
import logging
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ott.adapters import get_adapter
from ott.sandbox import DockerSandbox, InfraError, LocalSandbox
from ott.store import Lease, LeaseLost, Store, StoreError

LOG = logging.getLogger("ott.runner")
MAX_LINE = 1024 * 1024
MAX_STDOUT_TOTAL = 16 * 1024 * 1024
MAX_STDERR_TOTAL = 1024 * 1024
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Pause after every accepted move so spectators can follow; never charged to bot clocks.
# Default 2 s; override with `--move-delay-ms`.
MOVE_DELAY_S = 2.0

# Active execution backend; replaced by the CLI (`--runner`).
SANDBOX: LocalSandbox | DockerSandbox = LocalSandbox()
_REBUILD_LOCK = threading.Lock()

# live per-match info for the API: {match_id: {"side": str|None, "since": monotonic, "clocks": {...}}}
LIVE: dict[str, dict[str, Any]] = {}
LIVE_LOCK = threading.Lock()

__all__ = ["InfraError", "SANDBOX", "LIVE", "LIVE_LOCK", "artifact_path", "compile_source", "ensure_binary", "MatchRunner"]


def set_sandbox(sandbox: LocalSandbox | DockerSandbox) -> None:
    global SANDBOX
    SANDBOX = sandbox


def artifact_path(data_dir: Path, bot_id: str) -> Path:
    return data_dir / "artifacts" / bot_id / SANDBOX.exe_name


def compile_source(out: Path, source: str) -> tuple[bool, str | None]:
    """Compile `source` (C++17) to `out` with the active sandbox. Return (ok, filtered compiler message)."""
    return SANDBOX.compile(out, source)


def ensure_binary(store: Store, data_dir: Path, bot_id: str) -> Path:
    """Binary for a submitted bot; rebuild from the stored source if it is missing
    (e.g. after switching between the local and Docker runner)."""
    exe = artifact_path(data_dir, bot_id)
    bot = store.get_bot(bot_id, with_source=True)
    if not bot:
        raise InfraError("submitted bot is missing")
    if exe.exists():
        SANDBOX.verify_artifact(exe, expected_source_hash=bot["source_sha256"])
        return exe
    if SANDBOX.kind == "docker":
        raise InfraError("submitted artifact is missing; restore a verified backup or submit a new approved version")
    with _REBUILD_LOCK:
        if exe.exists():
            SANDBOX.verify_artifact(exe, expected_source_hash=bot["source_sha256"])
            return exe
        if not bot or not bot["source"]:
            raise InfraError("binary missing and no source to rebuild it")
        owner = "rebuild-" + uuid.uuid4().hex
        try:
            with store.compile_slot(owner) as token:
                ok, msg = SANDBOX.compile(exe, bot["source"])
                store.assert_compile_slot(owner,token)
                if ok:
                    SANDBOX.verify_artifact(exe, expected_source_hash=bot["source_sha256"])
        except LeaseLost as exc:
            raise InfraError("compiler slot unavailable or expired before artifact acceptance") from exc
        if not ok:
            raise InfraError(f"rebuild failed: {(msg or '')[:120]}")
    return exe


class BotProcess:
    def __init__(self, exe: Path, label: str) -> None:
        self.label = label
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self.stderr_tail = b""
        self.overflow = False
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.container = f"ott-bot-{uuid.uuid4().hex[:12]}" if SANDBOX.kind == "docker" else None
        self.proc = subprocess.Popen(
            SANDBOX.bot_argv(exe, self.container), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=SANDBOX.cwd(exe), creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def wait_ready(self) -> None:
        SANDBOX.wait_ready(self.container)

    def _read_out(self) -> None:
        try:
            while True:
                line = self.proc.stdout.readline(MAX_LINE + 2)  # type: ignore[union-attr]
                if not line:
                    break
                self.stdout_bytes += len(line)
                if (len(line) > MAX_LINE + 1 or (len(line) > MAX_LINE and not line.endswith(b"\n"))
                        or self.stdout_bytes > MAX_STDOUT_TOTAL):
                    self.overflow = True
                    break
                try:
                    self.q.put_nowait(line)
                except queue.Full:
                    self.overflow = True
                    break
        except (OSError, ValueError):
            pass
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def _read_err(self) -> None:
        try:
            while True:
                chunk = self.proc.stderr.read1(4096)  # type: ignore[union-attr]
                if not chunk:
                    break
                self.stderr_bytes += len(chunk)
                self.stderr_tail = (self.stderr_tail + chunk)[-4096:]
                if self.stderr_bytes > MAX_STDERR_TOTAL:
                    self.overflow = True
                    break
        except (OSError, ValueError):
            pass

    def send(self, line: str, timeout: float = 1.0) -> bool:
        completed = threading.Event()
        sent = []
        def write() -> None:
            try:
                self.proc.stdin.write(line.encode("utf-8") + b"\n")  # type: ignore[union-attr]
                self.proc.stdin.flush()  # type: ignore[union-attr]
                sent.append(True)
            except (OSError, ValueError):
                pass
            finally:
                completed.set()
        threading.Thread(target=write,daemon=True).start()
        return completed.wait(timeout=max(0.001,timeout)) and bool(sent)

    def has_pending(self) -> bool:
        return not self.q.empty()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass
        SANDBOX.kill(self.container)
        try:
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()  # type: ignore[union-attr]
            except (OSError, ValueError, AttributeError):
                pass
        try:
            self.proc.wait(timeout=5)  # reap the process / docker client
        except (subprocess.TimeoutExpired, OSError):
            pass


class MatchRunner:
    def __init__(self, store: Store, data_dir: Path, match_id: str, *, lease: Lease,
                 stop_event: threading.Event | None = None) -> None:
        self.store, self.data_dir, self.mid = store, data_dir, match_id
        self.lease = lease
        self.stop_event = stop_event or threading.Event()
        self.lease_lost = threading.Event()
        self.heartbeat_stop = threading.Event()

    def _heartbeat(self) -> None:
        while not self.heartbeat_stop.wait(5):
            try:
                self.store.heartbeat(self.mid,self.lease)
            except Exception:
                self.lease_lost.set()
                return

    def _check_control(self) -> None:
        if self.lease_lost.is_set():
            raise LeaseLost("Worker lost its execution lease.")
        if self.stop_event.is_set():
            raise InfraError("worker shutdown")

    def run(self) -> None:
        bots: dict[str, BotProcess] = {}
        heartbeat = threading.Thread(target=self._heartbeat,daemon=True,name=f"lease-{self.mid[:8]}")
        try:
            self.store.heartbeat(self.mid,self.lease)
            heartbeat.start()
            match = self.store.get_match(self.mid)
            if not match:
                raise InfraError("match disappeared")
            if match["execution_started"] or match["state_json"]:
                raise InfraError("REVIEW: persistent bot memory cannot be restored")
            snapshot = json.loads(match["execution_snapshot"])
            self.adapter = get_adapter(snapshot["game_id"],snapshot["game_version"])
            exes = {}
            artifacts = {}
            for side in ("white", "black"):
                self._check_control()
                eid = match[f"{side}_entry_id"]
                frozen = next((entry for entry in snapshot["entries"] if entry["id"] == eid),None)
                if not frozen or not frozen["bot_submission_id"]:
                    raise InfraError("immutable bot reference is missing")
                bot = self.store.get_bot(frozen["bot_submission_id"],with_source=True)
                if not bot or bot["source_sha256"] != frozen["source_sha256"]:
                    raise InfraError("submitted source hash differs from execution snapshot")
                exes[side] = ensure_binary(self.store,self.data_dir,frozen["bot_submission_id"])
                artifacts[side] = hashlib.sha256(exes[side].read_bytes()).hexdigest()
            self._check_control()
            self.store.start_execution(self.mid,self.lease,{
                "kind":SANDBOX.kind,"image":getattr(SANDBOX,"image",None),
                "compiler_image_id":getattr(SANDBOX,"compiler_image_id",None),
                "runtime_image_id":getattr(SANDBOX,"runtime_image_id",None),
                "artifacts":artifacts,"adapter":self.adapter.adapter_id,"version":self.adapter.version,
            })
            state = self.adapter.initial_state()
            clocks = {"white":match["white_clock_ms"],"black":match["black_clock_ms"]}
            outcome = self._play(match,exes,bots,state,clocks)
            self._check_control()
            self.store.finish_match(self.mid,outcome["result"],outcome["reason"],clocks,lease=self.lease)
        except LeaseLost:
            LOG.warning("match %s stopped after lease loss",self.mid)
        except Exception as exc:
            LOG.exception("match %s paused",self.mid)
            reason = f"INFRA: {str(exc)[:160]}" if isinstance(exc,InfraError) else f"REVIEW: {type(exc).__name__}"
            try:
                self.store.pause_match(self.mid,reason,lease=self.lease,retry=isinstance(exc,InfraError))
            except (LeaseLost,StoreError):
                pass
            except Exception:
                LOG.exception("could not persist pause for match %s",self.mid)
        finally:
            self.heartbeat_stop.set()
            for bot in bots.values():
                bot.kill()
            while heartbeat.is_alive():
                heartbeat.join(timeout=1)
            with LIVE_LOCK:
                LIVE.pop(self.mid,None)

    def _lose(self, side: str, reason: str) -> dict[str, str]:
        return self.adapter.loss(side,reason)

    def _play(self, match: dict[str, Any], exes: dict[str, Path], bots: dict[str, BotProcess],
              state: dict[str, Any], clocks: dict[str, int]) -> dict[str, str]:
        for side in ("white","black"):
            self._check_control()
            try:
                bots[side] = BotProcess(exes[side],side)
            except OSError as exc:
                raise InfraError("bot runtime could not start") from exc
        for side in ("white","black"):
            self._check_control()
            bots[side].wait_ready()
        seq = match["last_event_seq"]
        last_cp = time.monotonic()
        while True:
            self._check_control()
            side = self.adapter.actor(state)
            bot = bots[side]
            if bot.overflow:
                return self._lose(side,"OUTPUT_TOO_LARGE")
            if bot.has_pending():
                return self._lose(side,"EXTRA_OUTPUT")
            if clocks[side] <= 0:
                return self._lose(side,"TIMEOUT")
            observation = self.adapter.observation(state,side,self.mid,clocks)
            line = json.dumps(observation,separators=(",",":"))
            if len(line.encode("utf-8")) > MAX_LINE:
                raise InfraError("adapter observation exceeds protocol size")
            started = time.monotonic()
            with LIVE_LOCK:
                LIVE[self.mid] = {"side":side,"since":started,"clocks":dict(clocks)}
            sent = bot.send(line,timeout=min(1.0,clocks[side]/1000))
            self._check_control()
            if not sent:
                clocks[side] = max(0,clocks[side]-int((time.monotonic()-started)*1000))
                return self._lose(side,"BOT_INPUT_BLOCKED")
            raw: bytes | None = None
            got = False
            while True:
                self._check_control()
                if bot.overflow:
                    clocks[side] = max(0,clocks[side]-int((time.monotonic()-started)*1000))
                    return self._lose(side,"OUTPUT_TOO_LARGE")
                remaining = clocks[side]-(time.monotonic()-started)*1000
                if remaining <= 0:
                    break
                try:
                    raw = bot.q.get(timeout=min(0.1,remaining/1000))
                    got = True
                    break
                except queue.Empty:
                    if time.monotonic()-last_cp >= 1:
                        live = dict(clocks)
                        live[side] = max(0,int(remaining))
                        self.store.checkpoint(self.mid,live["white"],live["black"],lease=self.lease)
                        last_cp = time.monotonic()
            elapsed = int((time.monotonic()-started)*1000)
            clocks[side] = max(0,clocks[side]-elapsed) if got else 0
            if not got or clocks[side] <= 0:
                return self._lose(side,"TIMEOUT")
            if raw is None:
                if isinstance(SANDBOX,DockerSandbox) and bot.proc.poll() == 125:
                    raise InfraError("Docker execution failed after startup")
                return self._lose(side,"BOT_CRASH")
            if bot.overflow or len(raw) > MAX_LINE+1:
                return self._lose(side,"OUTPUT_TOO_LARGE")
            try:
                action = self.adapter.parse_action(raw.decode("utf-8").strip())
            except (ValueError,UnicodeDecodeError,RecursionError):
                return self._lose(side,"INVALID_ACTION")
            if not self.adapter.validate_action(state,action):
                return self._lose(side,"ILLEGAL_ACTION")
            phase = state["phase"]
            outcome = self.adapter.apply_action(state,action)
            seq += 1
            self.store.record_action(self.mid,seq,state,action,side,phase,clocks,lease=self.lease)
            last_cp = time.monotonic()
            if outcome:
                return outcome
            if MOVE_DELAY_S and phase == "play":
                with LIVE_LOCK:
                    LIVE[self.mid] = {"side":None,"since":time.monotonic(),"clocks":dict(clocks)}
                until = time.monotonic()+MOVE_DELAY_S
                while time.monotonic() < until:
                    self._check_control()
                    self.stop_event.wait(min(0.1,max(0,until-time.monotonic())))
