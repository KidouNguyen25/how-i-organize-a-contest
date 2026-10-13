"""Bot compile + interactive match runner.

DEV RUNNER: bots run as plain host subprocesses (wall-clock limit only, no CPU/RAM/PID limits,
no network isolation). Use only with trusted bots. A Docker runner must replace `start_bot`
and `compile_source` before running untrusted code (see docs/VibeSec.md).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ott import engine
from ott.store import Store

LOG = logging.getLogger("ott.runner")
MAX_LINE = 1024 * 1024
COMPILE_TIMEOUT = 15
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Pause after every accepted move so spectators can follow; never charged to bot clocks.
# Default 2 s; override with `--move-delay-ms`.
MOVE_DELAY_S = 2.0

# live per-match info for the API: {match_id: {"side": str, "since": monotonic}}
LIVE: dict[str, dict[str, Any]] = {}
LIVE_LOCK = threading.Lock()


class InfraError(Exception):
    """Failure not attributable to a bot; the match is paused, not forfeited."""


def artifact_path(data_dir: Path, bot_id: str) -> Path:
    exe = "bot.exe" if sys.platform == "win32" else "bot"
    return data_dir / "artifacts" / bot_id / exe


def compile_source(out: Path, source: str) -> tuple[bool, str | None]:
    """Compile `source` (C++17) to the executable `out`. Return (ok, filtered compiler message)."""
    compiler = shutil.which("g++") or shutil.which("c++")
    if not compiler:
        raise InfraError("g++ not found on PATH")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ott-cc-") as tmp:
        src = Path(tmp) / "bot.cpp"
        src.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [compiler, "-std=c++17", "-O2", "-o", str(out), str(src)],
                capture_output=True, text=True, timeout=COMPILE_TIMEOUT, cwd=tmp, creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return False, f"Compile timed out after {COMPILE_TIMEOUT}s."
        if proc.returncode != 0:
            msg = proc.stderr or proc.stdout or "compile failed"
            msg = msg.replace(str(src), "bot.cpp").replace(tmp, "")
            msg = re.sub(r"[A-Za-z]:[\\/][^\s:]*", "<path>", msg)
            return False, msg[:4000]
    return True, None


class BotProcess:
    def __init__(self, exe: Path, label: str) -> None:
        self.label = label
        self.q: queue.Queue[bytes | None] = queue.Queue()
        self.stderr_tail = b""
        self.overflow = False
        self.proc = subprocess.Popen(
            [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(exe.parent), creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_out(self) -> None:
        try:
            while True:
                line = self.proc.stdout.readline(MAX_LINE + 2)  # type: ignore[union-attr]
                if not line:
                    break
                if len(line) > MAX_LINE and not line.endswith(b"\n"):
                    self.overflow = True
                self.q.put(line)
        except (OSError, ValueError):
            pass
        self.q.put(None)

    def _read_err(self) -> None:
        try:
            for line in self.proc.stderr:  # type: ignore[union-attr]
                self.stderr_tail = (self.stderr_tail + line)[-4096:]
        except (OSError, ValueError):
            pass

    def send(self, line: str) -> bool:
        try:
            self.proc.stdin.write(line.encode("utf-8") + b"\n")  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]
            return True
        except (OSError, ValueError):
            return False

    def has_pending(self) -> bool:
        return not self.q.empty()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()  # type: ignore[union-attr]
            except (OSError, ValueError, AttributeError):
                pass


class MatchRunner:
    def __init__(self, store: Store, data_dir: Path, match_id: str) -> None:
        self.store, self.data_dir, self.mid = store, data_dir, match_id

    def run(self) -> None:
        match = self.store.get_match(self.mid)
        if not match:
            return
        bots: dict[str, BotProcess] = {}
        try:
            exes = {}
            for side in ("white", "black"):
                info = self.store.entry_bot_paths_info(match[f"{side}_entry_id"])
                if not info or not info["bot_id"]:
                    raise InfraError(f"{side} entry has no bot")
                exe = artifact_path(self.data_dir, info["bot_id"])
                if not exe.exists():
                    raise InfraError(f"binary missing for {side} bot")
                exes[side] = exe
            state = json.loads(match["state_json"]) if match["state_json"] else engine.new_state()
            clocks = {"white": match["white_clock_ms"], "black": match["black_clock_ms"]}
            outcome = self._play(match, exes, bots, state, clocks)
            self.store.finish_match(self.mid, outcome["result"], outcome["reason"], clocks)
        except InfraError as exc:
            LOG.warning("match %s paused: %s", self.mid, exc)
            self.store.pause_match(self.mid, f"INFRA: {exc}")
        except Exception as exc:  # noqa: BLE001
            LOG.exception("match %s crashed", self.mid)
            self.store.pause_match(self.mid, f"INFRA: {type(exc).__name__}")
        finally:
            for b in bots.values():
                b.kill()
            with LIVE_LOCK:
                LIVE.pop(self.mid, None)

    def _lose(self, side: str, reason: str) -> dict[str, str]:
        return {"result": "BLACK_WIN" if side == "white" else "WHITE_WIN", "reason": reason}

    def _play(self, match: dict[str, Any], exes: dict[str, Path], bots: dict[str, BotProcess], state: dict[str, Any], clocks: dict[str, int]) -> dict[str, str]:
        for side in ("white", "black"):
            try:
                bots[side] = BotProcess(exes[side], side)
            except OSError:
                return self._lose(side, "BOT_START_FAILED")
        seq = len(state["history"])
        last_cp = time.monotonic()
        while True:
            side = state["side"]
            bot = bots[side]
            if bot.has_pending():
                return self._lose(side, "EXTRA_OUTPUT")
            if clocks[side] <= 0:
                return self._lose(side, "TIMEOUT")
            obs = engine.observation(state, side, self.mid, clocks)
            line = json.dumps(obs, separators=(",", ":"))
            if len(line.encode()) > MAX_LINE:
                raise InfraError("observation too large")
            started = time.monotonic()
            with LIVE_LOCK:
                LIVE[self.mid] = {"side": side, "since": started, "clocks": dict(clocks)}
            if not bot.send(line):
                return self._lose(side, "BOT_CRASH")
            raw: bytes | None = b""
            got = False
            while True:
                remaining = clocks[side] - (time.monotonic() - started) * 1000
                if remaining <= 0:
                    break
                try:
                    raw = bot.q.get(timeout=min(1.0, remaining / 1000))
                    got = True
                    break
                except queue.Empty:
                    if time.monotonic() - last_cp >= 1.0:
                        live = dict(clocks)
                        live[side] = max(0, int(clocks[side] - (time.monotonic() - started) * 1000))
                        self.store.checkpoint(self.mid, live["white"], live["black"])
                        last_cp = time.monotonic()
            elapsed = int((time.monotonic() - started) * 1000)
            clocks[side] = max(0, clocks[side] - elapsed) if got else 0
            if not got or clocks[side] <= 0:
                return self._lose(side, "TIMEOUT")
            if raw is None:
                return self._lose(side, "BOT_CRASH")
            if bot.overflow or len(raw) > MAX_LINE + 1:
                return self._lose(side, "OUTPUT_TOO_LARGE")
            try:
                action = engine.parse_action(raw.decode("utf-8").strip())
            except (ValueError, UnicodeDecodeError):
                return self._lose(side, "INVALID_ACTION")
            if not engine.validate_action(state, action):
                return self._lose(side, "ILLEGAL_ACTION")
            phase = state["phase"]
            outcome = engine.apply_action(state, action)
            seq += 1
            self.store.record_action(self.mid, seq, state, action, side, phase, clocks)
            last_cp = time.monotonic()
            if outcome:
                return outcome
            if MOVE_DELAY_S and phase == "play":  # placements are secret, nothing to follow
                with LIVE_LOCK:  # clocks stand still during the pause
                    LIVE[self.mid] = {"side": None, "since": time.monotonic(), "clocks": dict(clocks)}
                time.sleep(MOVE_DELAY_S)
