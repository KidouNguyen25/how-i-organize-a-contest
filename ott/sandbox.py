"""Compile and run bots with bounded I/O and verified immutable artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Iterable

from core.languages import LANGUAGES, language_profile, valid_jar

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
COMPILE_OUTER_TIMEOUT = 60
IMAGE = "ott-bot-runner"
RUN_MEMORY_MB = 256
RUN_PIDS = 64
COMPILE_MEMORY_MB = 1024
COMPILE_PIDS = 128
MAX_SOURCE_BYTES = 256 * 1024
MAX_BINARY_BYTES = 64 * 1024 * 1024
MAX_COMPILER_OUTPUT_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 4096


class InfraError(Exception):
    """Failure not attributable to a bot; pause the match instead of forfeiting it."""


class SourcePolicyError(ValueError):
    """The submitted source is outside the configured admission policy."""


def _filesystem_errors(method: Any) -> Any:
    @wraps(method)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except OSError as exc:
            raise InfraError(f"artifact/build filesystem unavailable: {exc.strerror or type(exc).__name__}") from None
    return wrapped


def _clean_compiler_output(msg: str, *hidden: str) -> str:
    for item in hidden:
        msg = msg.replace(item, "")
    msg = re.sub(r"[A-Za-z]:[\\/][^\s:]*", "<path>", msg)
    return msg.encode("utf-8", errors="replace")[:MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace")


def _checked_path(path: Path) -> Path:
    """Reject symlinks before resolving any existing path component."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            mode = item.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or (hasattr(item, "is_junction") and item.is_junction()):
            raise InfraError("artifact paths must not contain links or junctions")
    return path


def _manifest_path(exe: Path) -> Path:
    return exe.with_name(exe.name + ".manifest.json")


def _hash_binary(exe: Path, *, linux: bool, artifact_format: str = "native",
                 main_class: str | None = None) -> tuple[str, int]:
    exe = _checked_path(exe)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(exe, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BINARY_BYTES:
                raise InfraError("artifact must be a nonempty regular file no larger than 64 MiB")
            if artifact_format == "jar":
                stream.seek(0)
                if not main_class or not valid_jar(stream, main_class):
                    raise InfraError("artifact is not a valid JAR with the expected main class")
                stream.seek(0)
            header = stream.read(64)
            if artifact_format != "jar" and linux:
                if (len(header) < 52 or header[:4] != b"\x7fELF" or header[4] not in (1, 2)
                        or header[5] not in (1, 2) or header[6] != 1
                        or (header[4] == 2 and len(header) < 64)):
                    raise InfraError("artifact is not a valid ELF executable")
                byteorder = "little" if header[5] == 1 else "big"
                if int.from_bytes(header[16:18], byteorder) not in (2, 3):
                    raise InfraError("ELF artifact must be an executable or position-independent executable")
            if artifact_format != "jar" and not linux and header[:2] != b"MZ":
                raise InfraError("local Windows artifact is not an executable")
            digest = hashlib.sha256(header)
            count = len(header)
            while chunk := stream.read(65536):
                count += len(chunk)
                if count > MAX_BINARY_BYTES:
                    raise InfraError("artifact exceeds 64 MiB")
                digest.update(chunk)
            if count != info.st_size:
                raise InfraError("artifact changed during verification")
            return digest.hexdigest(), count
    except OSError as exc:
        raise InfraError(f"cannot read artifact: {exc.strerror or type(exc).__name__}") from None


def _publish_artifact(source: Path, dest: Path, manifest: dict[str, Any]) -> None:
    """Publish binary first and manifest last; readers reject incomplete pairs."""
    dest = _checked_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _checked_path(dest.parent)
    manifest_dest = _checked_path(_manifest_path(dest))
    binary_tmp: Path | None = None
    manifest_tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".artifact-", dir=dest.parent, delete=False) as output:
            binary_tmp = Path(output.name)
            with source.open("rb") as incoming:
                count = 0
                digest = hashlib.sha256()
                while chunk := incoming.read(65536):
                    count += len(chunk)
                    if count > MAX_BINARY_BYTES:
                        raise InfraError("artifact exceeds 64 MiB")
                    digest.update(chunk)
                    output.write(chunk)
            if count != manifest["binary_size"] or digest.hexdigest() != manifest["binary_sha256"]:
                raise InfraError("artifact changed while publishing")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(binary_tmp, 0o555)
        with tempfile.NamedTemporaryFile(prefix=".manifest-", dir=dest.parent, delete=False, mode="w", encoding="utf-8") as output:
            manifest_tmp = Path(output.name)
            json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        # Windows read-only attributes prevent ordinary cache cleanup; ACLs protect its parent.
        os.chmod(manifest_tmp, 0o600 if os.name == "nt" else 0o444)
        try:
            # Atomic creation without replacing an existing immutable binary.
            os.link(binary_tmp, dest)
        except FileExistsError:
            raise InfraError("artifact destination already exists; create a new immutable version") from None
        if os.name == "nt":
            os.chmod(binary_tmp, 0o755)
        binary_tmp.unlink()
        binary_tmp = None
        os.replace(manifest_tmp, manifest_dest)
        manifest_tmp = None
        if os.name == "posix":
            directory = os.open(dest.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        raise InfraError(f"cannot publish artifact: {exc.strerror or type(exc).__name__}") from None
    finally:
        for item in (binary_tmp, manifest_tmp):
            if item is not None:
                if os.name == "nt" and item.exists():
                    os.chmod(item, 0o600)
                item.unlink(missing_ok=True)


def _bounded_process(args: list[str], *, binary_output: Any = None, cwd: str | None = None,
                     kill_container: Any = None, timeout: float = COMPILE_OUTER_TIMEOUT,
                     env: dict[str, str] | None = None) -> tuple[int, str, str | None]:
    """Drain pipes in bounded chunks; compiler diagnostics never accumulate unboundedly."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                cwd=cwd, env=env, creationflags=CREATE_NO_WINDOW, start_new_session=os.name == "posix")
    except OSError as exc:
        raise InfraError(f"compiler process unavailable: {exc.strerror or type(exc).__name__}") from None
    chunks: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=8)
    stopped = threading.Event()

    def read_pipe(label: str, pipe: Any) -> None:
        try:
            while not stopped.is_set():
                data = pipe.read1(65536)
                if not data:
                    break
                while not stopped.is_set():
                    try:
                        chunks.put((label, data), timeout=0.1)
                        break
                    except queue.Full:
                        pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
            while not stopped.is_set():
                try:
                    chunks.put((label, None), timeout=0.1)
                    break
                except queue.Full:
                    pass

    threads = [threading.Thread(target=read_pipe, args=(label, pipe), daemon=True)
               for label, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr))]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    diagnostic = bytearray()
    diagnostic_size = binary_size = finished = 0
    failure = None
    try:
        while finished < 2:
            if time.monotonic() >= deadline:
                failure = "outer timeout"
                break
            try:
                label, data = chunks.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if data is None:
                finished += 1
            elif label == "stdout" and binary_output is not None:
                binary_size += len(data)
                if binary_size > MAX_BINARY_BYTES:
                    failure = "binary output exceeds 64 MiB"
                    break
                binary_output.write(data)
            else:
                diagnostic_size += len(data)
                diagnostic.extend(data[:max(0, MAX_DIAGNOSTIC_BYTES - len(diagnostic))])
                if diagnostic_size > MAX_COMPILER_OUTPUT_BYTES:
                    failure = "compiler output exceeds 1 MiB"
                    break
        if failure is None:
            try:
                code = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = "outer timeout"
        if failure is not None:
            if kill_container is not None:
                kill_container()
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            code = -1
        return code, diagnostic.decode("utf-8", errors="replace"), failure
    finally:
        stopped.set()
        if proc.poll() is None:
            if kill_container is not None:
                kill_container()
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for thread in threads:
            thread.join(timeout=1)


class _ArtifactPolicy:
    kind: str

    def set_allowed_source_hashes(self, hashes: Iterable[str] | None) -> None:
        if hashes is None:
            self.allowed_source_hashes: frozenset[str] | None = None
            return
        values = frozenset(value.lower() for value in hashes)
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
            raise ValueError("source allowlist requires SHA-256 hex digests")
        self.allowed_source_hashes = values

    def source_allowed(self, source: str) -> bool:
        try:
            self.check_source(source)
            return True
        except SourcePolicyError:
            return False

    def check_source(self, source: str) -> None:
        if not isinstance(source, str):
            raise SourcePolicyError("source must be UTF-8 text")
        try:
            raw = source.encode("utf-8")
        except UnicodeError:
            raise SourcePolicyError("source must be valid UTF-8 text") from None
        if len(raw) > MAX_SOURCE_BYTES:
            raise SourcePolicyError("source exceeds 256 KiB")
        if b"\x00" in raw:
            raise SourcePolicyError("source must not contain NUL bytes")
        if self.allowed_source_hashes is not None and hashlib.sha256(raw).hexdigest() not in self.allowed_source_hashes:
            raise SourcePolicyError("source hash is not approved for this demo")

    def policy_info(self) -> dict[str, Any]:
        return {"kind": self.kind, "source_policy": "allowlist" if self.allowed_source_hashes is not None else "unrestricted",
                "allowed_source_count": len(self.allowed_source_hashes) if self.allowed_source_hashes is not None else None,
                "compiler_image_id": getattr(self, "compiler_image_id", None),
                "runtime_image_id": getattr(self, "runtime_image_id", None)}

    @_filesystem_errors
    def verify_artifact(self, exe: Path, expected_source_hash: str | None = None,
                        expected_language: str | None = None) -> dict[str, Any]:
        exe = _checked_path(exe)
        path = _checked_path(_manifest_path(exe))
        try:
            if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 65536:
                raise InfraError("invalid artifact manifest")
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise InfraError("artifact manifest missing or invalid; rebuild as a new approved version") from None
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("sandbox_kind") != self.kind:
            raise InfraError("artifact manifest version or execution mode mismatch")
        source_hash = manifest.get("source_sha256", "")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise InfraError("artifact source hash missing or invalid")
        if expected_source_hash is not None and source_hash != expected_source_hash:
            raise InfraError("artifact does not match the selected source")
        if self.allowed_source_hashes is not None and source_hash not in self.allowed_source_hashes:
            raise InfraError("artifact source is outside the approved demo allowlist")
        language = manifest.get("language", "cpp17")
        try:
            profile = language_profile(language)
        except ValueError:
            raise InfraError("artifact language is not supported") from None
        if expected_language is not None and language != expected_language:
            raise InfraError("artifact language does not match the selected submission")
        if manifest.get("compile_flags") != list(profile.flags):
            raise InfraError("artifact compiler flags mismatch")
        if manifest.get("artifact_format", "native") != profile.artifact_format:
            raise InfraError("artifact format mismatch")
        if manifest.get("compile_environment", []) != [list(item) for item in profile.environment]:
            raise InfraError("artifact compiler environment mismatch")
        if self.kind == "docker":
            self._ensure_images()
            if (manifest.get("compiler_image_id") != self.compiler_image_id
                    or manifest.get("runtime_image_id") != self.runtime_image_id):
                raise InfraError("artifact image mismatch; create a new approved build")
        binary_hash, binary_size = _hash_binary(
            exe, linux=self.kind == "docker" or os.name == "posix",
            artifact_format=profile.artifact_format, main_class=profile.main_class,
        )
        if manifest.get("binary_sha256") != binary_hash or manifest.get("binary_size") != binary_size:
            raise InfraError("artifact integrity check failed")
        return manifest

    @_filesystem_errors
    def copy_artifact(self, source: Path, dest: Path, expected_source_hash: str | None = None,
                      expected_language: str | None = None) -> dict[str, Any]:
        manifest = self.verify_artifact(source, expected_source_hash, expected_language)
        _publish_artifact(_checked_path(source), dest, manifest)
        return self.verify_artifact(dest, expected_source_hash, expected_language)

    def _manifest(self, exe: Path, source: str, language: str = "cpp17") -> dict[str, Any]:
        profile = language_profile(language)
        digest, size = _hash_binary(
            exe, linux=self.kind == "docker" or os.name == "posix",
            artifact_format=profile.artifact_format, main_class=profile.main_class,
        )
        return {"schema_version": 1, "sandbox_kind": self.kind,
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "binary_sha256": digest, "binary_size": size, "language": language,
                "artifact_format": profile.artifact_format,
                "compile_flags": list(profile.flags),
                "compile_environment": [list(item) for item in profile.environment],
                "compiler_image_id": getattr(self, "compiler_image_id", None),
                "runtime_image_id": getattr(self, "runtime_image_id", None)}


class LocalSandbox(_ArtifactPolicy):
    """Explicit development mode; ordinary host processes have no security isolation."""

    kind = "local"

    def __init__(self, *, allowed_source_hashes: Iterable[str] | None = None) -> None:
        self.set_allowed_source_hashes(allowed_source_hashes)

    @property
    def exe_name(self) -> str:
        return "bot.exe" if sys.platform == "win32" else "bot"

    def available(self) -> tuple[bool, str]:
        found = [profile.compiler for profile in LANGUAGES.values()
                 if shutil.which(profile.compiler)
                 and (not profile.runtime or shutil.which(profile.runtime[0]))
                 and (profile.artifact_format != "jar" or shutil.which("jar"))]
        if not found and shutil.which("c++"):
            found.append("c++")
        return bool(found), (", ".join(found) + " found (unisolated development mode)"
                             if found else "No supported compiler toolchain found on PATH")

    @_filesystem_errors
    def compile(self, out: Path, source: str, language: str = "cpp17") -> tuple[bool, str | None]:
        self.check_source(source)
        profile = language_profile(language)
        compiler = shutil.which(profile.compiler)
        if language == "cpp17" and not compiler:
            compiler = shutil.which("c++")
        if not compiler:
            raise InfraError(f"{profile.compiler} not found on PATH")
        _checked_path(out)
        with tempfile.TemporaryDirectory(prefix="ott-cc-") as tmp:
            src, built = Path(tmp) / profile.filename, Path(tmp) / self.exe_name
            compile_output = Path(tmp) / "classes" if profile.artifact_format == "jar" else built
            if profile.artifact_format == "jar":
                compile_output.mkdir()
            src.write_bytes(source.encode("utf-8"))
            command = profile.command(str(src), str(compile_output))
            command[0] = compiler
            code, diagnostic, failure = _bounded_process(
                command, cwd=tmp, timeout=profile.compile_timeout_seconds,
                env={**os.environ, **dict(profile.environment)})
            if failure is not None:
                return False, failure
            if code != 0:
                return False, _clean_compiler_output(diagnostic or "compile failed", str(src), tmp)
            if profile.artifact_format == "jar":
                if not (compile_output / "Main.class").is_file():
                    return False, "Java source must declare Main in the default package."
                package = profile.package_command(str(compile_output), str(built))
                packager = shutil.which(package[0])
                if not packager:
                    raise InfraError("jar not found on PATH")
                package[0] = packager
                code, diagnostic, failure = _bounded_process(package, cwd=tmp, timeout=10)
                if failure is not None:
                    return False, failure
                if code != 0:
                    return False, _clean_compiler_output(diagnostic or "compile failed", tmp)
            self.check_source(source)
            _publish_artifact(built, out, self._manifest(built, source, language))
        return True, None

    def bot_argv(self, exe: Path, container: str | None = None) -> list[str]:
        manifest = self.verify_artifact(exe)
        profile = language_profile(manifest["language"])
        return profile.run_command(str(_checked_path(exe)))

    def cwd(self, exe: Path) -> str | None:
        return str(exe.parent)

    def wait_ready(self, container: str | None) -> None:
        return None

    def kill(self, container: str | None) -> None:
        return None


class DockerSandbox(_ArtifactPolicy):
    """Dedicated compile/runtime containers; images are pinned to inspected local IDs."""

    kind = "docker"
    exe_name = "bot"

    def __init__(self, image: str = IMAGE, *, runtime_image: str | None = None,
                 allowed_source_hashes: Iterable[str] | None = None) -> None:
        self.image = image
        self.runtime_image = runtime_image or image
        self.compiler_image_id: str | None = None
        self.runtime_image_id: str | None = None
        self._image_lock = threading.Lock()
        self.set_allowed_source_hashes(allowed_source_hashes)

    def _inspect_image(self, reference: str) -> str:
        try:
            result = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", reference],
                                    capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            raise InfraError("cannot inspect local Docker image") from None
        if result.returncode != 0:
            raise InfraError(f"required local image '{reference}' unavailable; images are never pulled by the runner")
        try:
            info = json.loads(result.stdout)
            image_id = info["Id"]
            if info.get("Os") != "linux" or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ValueError("unsupported image")
            return image_id
        except (ValueError, KeyError, TypeError):
            raise InfraError("Docker image inspection returned an invalid Linux image identity") from None

    def _ensure_images(self) -> None:
        with self._image_lock:
            if self.compiler_image_id is None:
                self.compiler_image_id = self._inspect_image(self.image)
            if self.runtime_image_id is None:
                self.runtime_image_id = (self.compiler_image_id if self.runtime_image == self.image
                                         else self._inspect_image(self.runtime_image))

    def available(self) -> tuple[bool, str]:
        if not shutil.which("docker"):
            return False, "docker CLI not found on PATH"
        try:
            result = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True,
                                    text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
            if result.returncode != 0 or result.stdout.strip() != "linux":
                return False, "Linux Docker daemon unavailable"
            self._ensure_images()
        except (OSError, subprocess.TimeoutExpired, InfraError) as exc:
            return False, str(exc) if isinstance(exc, InfraError) else "docker daemon not reachable"
        return True, f"Linux Docker / compiler {self.compiler_image_id} / runtime {self.runtime_image_id}"

    def run_args(self, name: str, *, memory_mb: int, pids: int, tmp_mb: int,
                 tmp_exec: bool, user: str = "65534:65534") -> list[str]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise InfraError("invalid container name")
        return ["docker", "run", "--rm", "--pull=never", "--name", name, "--network=none",
                f"--memory={memory_mb}m", f"--memory-swap={memory_mb}m", "--cpus=1", f"--pids-limit={pids}",
                "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", f"--user={user}",
                f"--tmpfs=/tmp:rw,{'exec' if tmp_exec else 'noexec'},nosuid,nodev,size={tmp_mb}m",
                "--ulimit=nofile=64:64", "--ulimit=fsize=67108864:67108864", "--ulimit=core=0", "--env=HOME=/tmp"]

    @_filesystem_errors
    def compile(self, out: Path, source: str, language: str = "cpp17") -> tuple[bool, str | None]:
        self.check_source(source)
        profile = language_profile(language)
        self._ensure_images()
        _checked_path(out)
        name = f"ott-cc-{uuid.uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory(prefix="ott-cc-") as tmp:
            src_dir, built = Path(tmp) / "src", Path(tmp) / "bot"
            src_dir.mkdir(mode=0o755)
            src = src_dir / profile.filename
            src.write_bytes(source.encode("utf-8"))
            os.chmod(src, 0o444)
            src_dir = _checked_path(src_dir)
            if "," in str(src_dir):
                raise InfraError("Docker mount path must not contain a comma")
            args = self.run_args(name, memory_mb=COMPILE_MEMORY_MB, pids=COMPILE_PIDS,
                                 tmp_mb=profile.compile_tmp_mb, tmp_exec=True)
            for key, value in profile.environment:
                args += ["--env", f"{key}={value}"]
            if profile.artifact_format == "jar":
                command = shlex.join(profile.command("/src/" + profile.filename, "/tmp/classes"))
                package = shlex.join(profile.package_command("/tmp/classes", "/build/bot"))
                program = (
                    "mkdir -p /tmp/classes && "
                    f"timeout --signal=KILL {profile.compile_timeout_seconds}s {command} 1>&2; "
                    'status=$?; if [ "$status" -ne 0 ]; then exit "$status"; fi; '
                    'test -f /tmp/classes/Main.class || exit 1; '
                    f"{package} 1>&2; status=$?; if [ \"$status\" -ne 0 ]; then exit \"$status\"; fi; "
                    'test -f /build/bot && test ! -L /build/bot || exit 126; exec cat /build/bot'
                )
            else:
                command = shlex.join(profile.command("/src/" + profile.filename, "/build/bot"))
                program = (
                    f"timeout --signal=KILL {profile.compile_timeout_seconds}s {command} 1>&2; "
                    'status=$?; if [ "$status" -ne 0 ]; then exit "$status"; fi; '
                    'test -f /build/bot && test ! -L /build/bot || exit 126; exec cat /build/bot'
                )
            args += ["--tmpfs=/build:rw,noexec,nosuid,nodev,size=64m,mode=0700,uid=65534,gid=65534",
                     "--mount", f"type=bind,src={src_dir},dst=/src,readonly",
                     self.compiler_image_id, "sh", "-c", program]
            with built.open("wb") as binary:
                code, diagnostic, failure = _bounded_process(args, binary_output=binary,
                                                            kill_container=lambda: self.kill(name))
            if failure == "outer timeout":
                raise InfraError("Docker build exceeded the 60s infrastructure deadline")
            if failure is not None:
                return False, failure
            if code == 124:
                return False, f"Compile timed out after {profile.compile_timeout_seconds}s."
            if code == 137:
                return False, "Compiler exceeded its time or resource limit."
            if code in (125, 126, 127):
                raise InfraError("Docker build environment failed; inspect restricted operator logs")
            if code != 0:
                return False, _clean_compiler_output(diagnostic or "compile failed", "/src/", tmp)
            self.check_source(source)
            _publish_artifact(built, out, self._manifest(built, source, language))
        return True, None

    def bot_argv(self, exe: Path, container: str | None = None) -> list[str]:
        manifest = self.verify_artifact(exe)
        profile = language_profile(manifest["language"])
        exe = _checked_path(exe)
        if "," in str(exe):
            raise InfraError("Docker mount path must not contain a comma")
        name = container or f"ott-bot-{uuid.uuid4().hex[:12]}"
        args = self.run_args(name, memory_mb=RUN_MEMORY_MB, pids=RUN_PIDS, tmp_mb=16, tmp_exec=False)
        args.insert(args.index("--rm") + 1, "-i")
        return args + ["--mount", f"type=bind,src={exe},dst=/bot/bot,readonly",
                       self.runtime_image_id, *profile.run_command("/bot/bot")]

    def cwd(self, exe: Path) -> str | None:
        return None

    def wait_ready(self, container: str | None, timeout: float = 20.0) -> None:
        if not container:
            raise InfraError("Docker container identity missing")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", container],
                                        capture_output=True, text=True,
                                        timeout=max(0.05, min(2.0, deadline - time.monotonic())),
                                        creationflags=CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                self.kill(container)
                raise InfraError("cannot inspect Docker runtime readiness") from None
            if result.returncode == 0:
                try:
                    state = json.loads(result.stdout)
                except (ValueError, TypeError):
                    raise InfraError("invalid Docker runtime state") from None
                if not isinstance(state, dict):
                    raise InfraError("invalid Docker runtime state")
                if state.get("Running"):
                    return
                if state.get("Status") in ("exited", "dead", "removing"):
                    raise InfraError("Docker container exited before readiness; bot verdict requires runtime evidence")
            elif "no such" not in (result.stderr or "").lower():
                self.kill(container)
                raise InfraError("Docker runtime inspection failed")
            time.sleep(0.05)
        self.kill(container)
        raise InfraError("Docker container did not become ready within the startup deadline")

    def kill(self, container: str | None) -> None:
        if not container or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
            return
        try:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass


def make_sandbox(kind: str, image: str = IMAGE, *, runtime_image: str | None = None,
                 allowed_source_hashes: Iterable[str] | None = None) -> LocalSandbox | DockerSandbox:
    if kind == "local":
        return LocalSandbox(allowed_source_hashes=allowed_source_hashes)
    if kind in ("docker", "auto"):
        sandbox = DockerSandbox(image, runtime_image=runtime_image, allowed_source_hashes=allowed_source_hashes)
        if kind == "auto":
            ok, detail = sandbox.available()
            if not ok:
                raise InfraError(f"Docker required; automatic local fallback is disabled: {detail}")
        return sandbox
    raise ValueError(f"unknown runner: {kind}")
