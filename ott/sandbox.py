"""Where bots are compiled and executed: plain host processes (dev) or Docker containers (sandbox)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
COMPILE_TIMEOUT = 15
IMAGE = "ott-bot-runner"
RUN_MEMORY_MB = 256
RUN_PIDS = 64
COMPILE_MEMORY_MB = 1024
COMPILE_PIDS = 128


class InfraError(Exception):
    """Failure not attributable to a bot; the match is paused, not forfeited."""


def _clean_compiler_output(msg: str, *hidden: str) -> str:
    for h in hidden:
        msg = msg.replace(h, "")
    return re.sub(r"[A-Za-z]:[\\/][^\s:]*", "<path>", msg)[:4000]


class LocalSandbox:
    """DEV ONLY: bots run as ordinary host processes. No CPU/RAM/PID limits, no network isolation."""

    kind = "local"

    @property
    def exe_name(self) -> str:
        return "bot.exe" if sys.platform == "win32" else "bot"

    def available(self) -> tuple[bool, str]:
        ok = bool(shutil.which("g++") or shutil.which("c++"))
        return ok, "g++ found" if ok else "g++ not found on PATH"

    def compile(self, out: Path, source: str) -> tuple[bool, str | None]:
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
                return False, _clean_compiler_output(msg.replace(str(src), "bot.cpp"), tmp)
        return True, None

    def bot_argv(self, exe: Path, container: str | None = None) -> list[str]:
        return [str(exe)]

    def cwd(self, exe: Path) -> str | None:
        return str(exe.parent)

    def wait_ready(self, container: str | None) -> None:
        return None

    def kill(self, container: str | None) -> None:
        return None


class DockerSandbox:
    """Every compile and every bot process runs in its own throw-away container.

    Containers: no network, read-only root FS, all capabilities dropped, no-new-privileges, non-root user,
    1 CPU, 256 MiB RAM (swap disabled), 64 PIDs, small noexec tmpfs, file/descriptor ulimits.
    """

    kind = "docker"
    exe_name = "bot"  # always a Linux binary, whatever the host OS

    def __init__(self, image: str = IMAGE) -> None:
        self.image = image

    # ---- availability ----
    def available(self) -> tuple[bool, str]:
        docker = shutil.which("docker")
        if not docker:
            return False, "docker CLI not found on PATH"
        try:
            info = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return False, "docker daemon not reachable"
        if info.returncode != 0:
            return False, "docker daemon not reachable (is Docker running?)"
        img = subprocess.run([docker, "image", "inspect", self.image], capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW)
        if img.returncode != 0:
            return False, f"image '{self.image}' not built (run: python -m ott build-image)"
        return True, f"docker {info.stdout.strip()} / image {self.image}"

    # ---- container flags ----
    def run_args(self, name: str, *, memory_mb: int, pids: int, tmp_mb: int, tmp_exec: bool, user: str = "65534:65534") -> list[str]:
        return [
            "docker", "run", "--rm", "--name", name,
            "--network=none",
            f"--memory={memory_mb}m", f"--memory-swap={memory_mb}m",
            "--cpus=1", f"--pids-limit={pids}",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            f"--user={user}",
            f"--tmpfs=/tmp:rw,{'exec' if tmp_exec else 'noexec'},nosuid,nodev,size={tmp_mb}m",
            "--ulimit=nofile=64:64", "--ulimit=fsize=67108864:67108864", "--ulimit=core=0",
            "--env=HOME=/tmp",
        ]

    # ---- compile ----
    def compile(self, out: Path, source: str) -> tuple[bool, str | None]:
        name = f"ott-cc-{uuid.uuid4().hex[:12]}"
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ott-cc-") as tmp:
            src_dir, build_dir = Path(tmp) / "src", Path(tmp) / "build"
            src_dir.mkdir()
            build_dir.mkdir()
            (src_dir / "bot.cpp").write_text(source, encoding="utf-8")
            if os.name == "posix":  # container runs as nobody; the build dir must be writable by it
                os.chmod(build_dir, 0o777)
            args = self.run_args(name, memory_mb=COMPILE_MEMORY_MB, pids=COMPILE_PIDS, tmp_mb=64, tmp_exec=True)
            args += [
                "--mount", f"type=bind,src={src_dir},dst=/src,readonly",
                "--mount", f"type=bind,src={build_dir},dst=/build",
                self.image,
                "timeout", str(COMPILE_TIMEOUT), "g++", "-std=c++17", "-O2", "-pipe", "-o", "/build/bot", "/src/bot.cpp",
            ]
            try:
                proc = subprocess.run(args, capture_output=True, text=True, timeout=COMPILE_TIMEOUT + 45, creationflags=CREATE_NO_WINDOW)
            except subprocess.TimeoutExpired:
                self.kill(name)
                return False, f"Compile timed out after {COMPILE_TIMEOUT}s."
            except OSError as exc:
                raise InfraError(f"docker unavailable: {exc}") from None
            if proc.returncode == 124:
                return False, f"Compile timed out after {COMPILE_TIMEOUT}s."
            if proc.returncode != 0:
                text = proc.stderr or proc.stdout or "compile failed"
                if "docker:" in text.lower() and ("daemon" in text.lower() or "error response" in text.lower() or "pull access" in text.lower()):
                    raise InfraError(text.strip().splitlines()[0][:200])
                return False, _clean_compiler_output(text.replace("/src/bot.cpp", "bot.cpp"))
            built = build_dir / "bot"
            if not built.exists():
                raise InfraError("compiler produced no binary")
            shutil.copy2(built, out)
            os.chmod(out, 0o755)
        return True, None

    # ---- run ----
    def bot_argv(self, exe: Path, container: str | None = None) -> list[str]:
        name = container or f"ott-bot-{uuid.uuid4().hex[:12]}"
        args = self.run_args(name, memory_mb=RUN_MEMORY_MB, pids=RUN_PIDS, tmp_mb=16, tmp_exec=False)
        args.insert(args.index("--rm") + 1, "-i")
        args += ["--mount", f"type=bind,src={exe},dst=/bot/bot,readonly", self.image, "/bot/bot"]
        return args

    def cwd(self, exe: Path) -> str | None:
        return None

    def wait_ready(self, container: str | None, timeout: float = 20.0) -> None:
        """Block until the container is running so startup latency is not charged to the bot's clock."""
        if not container:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0 and res.stdout.strip() == "true":
                return
            time.sleep(0.05)

    def kill(self, container: str | None) -> None:
        if not container:
            return
        try:
            subprocess.run(["docker", "kill", container], capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass


def make_sandbox(kind: str, image: str = IMAGE) -> LocalSandbox | DockerSandbox:
    if kind == "docker":
        return DockerSandbox(image)
    if kind == "local":
        return LocalSandbox()
    if kind == "auto":
        docker = DockerSandbox(image)
        return docker if docker.available()[0] else LocalSandbox()
    raise ValueError(f"unknown runner: {kind}")
