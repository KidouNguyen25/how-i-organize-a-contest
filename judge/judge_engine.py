"""Compile and run submissions in short-lived, resource-limited Docker containers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from core.languages import language_profile, valid_jar
from judge.problems import Problem
from judge.scoring import output_tokens, summarize_cases


MAX_OUTPUT_BYTES = 1024 * 1024
MAX_COMPILE_LOG_BYTES = 256 * 1024
MAX_BINARY_BYTES = 64 * 1024 * 1024
RUNNER_MEMORY_MB = 1024
RUNNER_PIDS = 64


class JudgeInfrastructureError(RuntimeError):
    pass


class OutputLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ContainerResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limited: bool = False


class DockerJudge:
    def __init__(self, image: str = "lan-judge-cpp17", runtime_image: str | None = None) -> None:
        self.image = image
        self.runtime_image = runtime_image or image
        self.compiler_image_id: str | None = None
        self.runtime_image_id: str | None = None

    async def _image_id(self, reference: str) -> str:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "image", "inspect", "--format", "{{json .}}", reference,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, TimeoutError) as exc:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            raise JudgeInfrastructureError("Docker image inspection unavailable.") from exc
        if process.returncode != 0:
            raise JudgeInfrastructureError(f"Required local image {reference!r} is unavailable; automatic pulls are disabled.")
        try:
            info = json.loads(stdout)
            identity = info["Id"]
            if info.get("Os") != "linux" or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise JudgeInfrastructureError("Invalid Linux image identity.") from None
        return identity

    async def check_available(self) -> None:
        self.compiler_image_id = await self._image_id(self.image)
        self.runtime_image_id = (self.compiler_image_id if self.runtime_image == self.image
                                 else await self._image_id(self.runtime_image))

    async def judge(self, problem: Problem, source: str, language: str = "cpp17") -> dict[str, object]:
        profile = language_profile(language)
        if os.name != "posix":
            raise JudgeInfrastructureError("Docker judging requires a Linux server.")
        if self.compiler_image_id is None or self.runtime_image_id is None:
            await self.check_available()
        runner_uid, runner_gid = _container_identity()
        with tempfile.TemporaryDirectory(prefix="lan-judge-") as temporary:
            root = Path(temporary)
            source_dir = root / "source"
            build_dir = root / "build"
            source_dir.mkdir(mode=0o700)
            build_dir.mkdir(mode=0o700)
            source_path = source_dir / profile.filename
            source_path.write_text(source, encoding="utf-8")
            source_path.chmod(0o400)
            _set_owner(root, runner_uid, runner_gid)
            _set_owner(source_dir, runner_uid, runner_gid)
            _set_owner(source_path, runner_uid, runner_gid)
            _set_owner(build_dir, runner_uid, runner_gid)

            compile_timeout = max(problem.compile_timeout_sec, profile.compile_timeout_seconds)
            compile_result = await self._compile(
                source_dir=source_dir,
                build_dir=build_dir,
                work_dir=root,
                timeout=compile_timeout,
                language=language,
                uid=runner_uid,
                gid=runner_gid,
            )
            if compile_result.timed_out:
                return {
                    "verdict": "CE",
                    "score": 0,
                    "test_results": [],
                    "message": f"Compilation exceeded {compile_timeout} seconds.",
                }
            if compile_result.output_limited:
                return {
                    "verdict": "CE",
                    "score": 0,
                    "test_results": [],
                    "message": "Compiler output exceeded 256 KiB.",
                }
            if compile_result.returncode != 0:
                if _is_docker_error(compile_result.stderr):
                    raise JudgeInfrastructureError(_short_message(compile_result.stderr))
                return {
                    "verdict": "CE",
                    "score": 0,
                    "test_results": [],
                    "message": _short_message(compile_result.stderr or compile_result.stdout),
                }

            binary = build_dir / "main"
            info = binary.lstat() if binary.exists() or binary.is_symlink() else None
            if (info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or not 0 < info.st_size <= MAX_BINARY_BYTES):
                raise JudgeInfrastructureError("Compiler did not produce a valid binary under the size limit.")
            if profile.artifact_format == "jar":
                if not profile.main_class or not valid_jar(binary, profile.main_class):
                    raise JudgeInfrastructureError("Compiler output is not a valid JAR with the expected main class.")
            else:
                with binary.open("rb") as stream:
                    if stream.read(4) != b"\x7fELF":
                        raise JudgeInfrastructureError("Compiler output is not an ELF executable.")
            binary.chmod(0o500)
            case_results: list[dict[str, object]] = []
            for index, case in enumerate(problem.tests, start=1):
                result = await self._run_case(
                    binary=binary,
                    work_dir=root,
                    input_bytes=case.input.encode("utf-8"),
                    timeout=problem.time_limit_ms / 1000,
                    memory_mb=problem.memory_limit_mb,
                    language=language,
                    uid=runner_uid,
                    gid=runner_gid,
                )
                if result.timed_out:
                    verdict = "TLE"
                    message = "Time limit exceeded."
                elif result.output_limited:
                    verdict = "RE"
                    message = "Program output exceeded 1 MiB."
                elif _is_docker_error(result.stderr):
                    raise JudgeInfrastructureError(_short_message(result.stderr))
                elif result.returncode != 0:
                    verdict = "RE"
                    message = f"Program exited with code {result.returncode}."
                elif output_tokens(result.stdout) != output_tokens(case.output.encode("utf-8")):
                    verdict = "WA"
                    message = None
                else:
                    verdict = "AC"
                    message = None
                case_results.append(
                    {
                        "test": index,
                        "verdict": verdict,
                        "points": case.points,
                        "awarded_points": case.points if verdict == "AC" else 0,
                        "message": message,
                    }
                )

            verdict, score = summarize_cases(case_results)
            return {
                "verdict": verdict,
                "score": score,
                "test_results": case_results,
                "message": None,
            }

    async def _compile(
        self,
        *,
        source_dir: Path,
        build_dir: Path,
        work_dir: Path,
        timeout: int,
        language: str = "cpp17",
        uid: int,
        gid: int,
    ) -> ContainerResult:
        args = self._container_base(
            cid_file=work_dir / ("compile-" + uuid.uuid4().hex + ".cid"),
            memory_mb=RUNNER_MEMORY_MB,
            uid=uid,
            gid=gid,
            tmp_size_mb=max(64, language_profile(language).compile_tmp_mb),
        )
        args.extend(
            [
                "--mount",
                f"type=bind,src={source_dir},dst=/submission,readonly",
                "--mount",
                f"type=bind,src={build_dir},dst=/build",
                "--env",
                "HOME=/tmp",
            ]
        )
        profile = language_profile(language)
        for key, value in profile.environment:
            args.extend(["--env", f"{key}={value}"])
        if profile.artifact_format == "jar":
            command = shlex.join(profile.command("/submission/" + profile.filename, "/build/classes"))
            package = shlex.join(profile.package_command("/build/classes", "/build/main"))
            args.extend([self.compiler_image_id or self.image, "sh", "-c",
                         f"mkdir -p /build/classes && {command} && "
                         "test -f /build/classes/Main.class && " + package])
        else:
            args.extend([self.compiler_image_id or self.image,
                         *profile.command("/submission/" + profile.filename, "/build/main")])
        result = await _run_docker(args, timeout=float(timeout), input_bytes=None, output_limit=MAX_COMPILE_LOG_BYTES)
        if result.returncode != 0 and _is_docker_error(result.stderr):
            raise JudgeInfrastructureError(_short_message(result.stderr))
        return result

    async def _run_case(
        self,
        *,
        binary: Path,
        work_dir: Path,
        input_bytes: bytes,
        timeout: float,
        memory_mb: int,
        language: str,
        uid: int,
        gid: int,
    ) -> ContainerResult:
        args = self._container_base(
            cid_file=work_dir / ("run-" + uuid.uuid4().hex + ".cid"),
            memory_mb=memory_mb,
            uid=uid,
            gid=gid,
            tmp_size_mb=16,
            tmp_exec=False,
        )
        profile = language_profile(language)
        args.extend(["--mount", f"type=bind,src={binary},dst=/judge/main,readonly", "--interactive",
                     self.runtime_image_id or self.runtime_image, *profile.run_command("/judge/main")])
        return await _run_docker(args, timeout=timeout, input_bytes=input_bytes, output_limit=MAX_OUTPUT_BYTES)

    def _container_base(
        self,
        *,
        cid_file: Path,
        memory_mb: int,
        uid: int,
        gid: int,
        tmp_size_mb: int,
        tmp_exec: bool = True,
    ) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            f"--cidfile={cid_file}",
            "--network=none",
            f"--memory={memory_mb}m",
            f"--memory-swap={memory_mb}m",
            "--cpus=1",
            f"--pids-limit={RUNNER_PIDS}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--user={uid}:{gid}",
            f"--tmpfs=/tmp:rw,{'exec' if tmp_exec else 'noexec'},nosuid,nodev,size={tmp_size_mb}m",
            "--ulimit=core=0",
            "--ulimit=nofile=64:64",
            "--ulimit=fsize=67108864:67108864",
        ]


async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while chunk := await stream.read(65536):
        if len(output) + len(chunk) > limit:
            raise OutputLimitExceeded
        output.extend(chunk)
    return bytes(output)


async def _write_input(stream: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if stream is None:
        return
    try:
        if data:
            stream.write(data)
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
        try:
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _stop_process(process: asyncio.subprocess.Process, cid_file: Path) -> None:
    if process.returncode is None:
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            pass
    container_id = None
    for _ in range(10):
        try:
            container_id = cid_file.read_text(encoding="ascii").strip()
            if container_id:
                break
        except OSError:
            pass
        await asyncio.sleep(0.1)
    if container_id and all(char in "0123456789abcdef" for char in container_id.lower()):
        try:
            killer = await asyncio.create_subprocess_exec(
                "docker", "kill", container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=3)
        except (OSError, TimeoutError):
            pass


async def _run_docker(
    args: list[str],
    *,
    timeout: float,
    input_bytes: bytes | None,
    output_limit: int,
) -> ContainerResult:
    cid_argument = next(item for item in args if item.startswith("--cidfile="))
    cid_file = Path(cid_argument.split("=", 1)[1])
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise JudgeInfrastructureError("Could not start Docker CLI.") from exc

    tasks: list[asyncio.Task] = []
    try:
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(_read_bounded(process.stdout, output_limit))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr, output_limit))
        input_task = asyncio.create_task(_write_input(process.stdin, input_bytes))
        wait_task = asyncio.create_task(process.wait())
        tasks = [stdout_task, stderr_task, input_task, wait_task]
        stdout, stderr, _, returncode = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task, input_task, wait_task),
            timeout=timeout,
        )
        return ContainerResult(returncode=returncode, stdout=stdout, stderr=stderr)
    except TimeoutError:
        await _stop_process(process, cid_file)
        await _cancel_tasks(tasks)
        return ContainerResult(returncode=-1, stdout=b"", stderr=b"", timed_out=True)
    except OutputLimitExceeded:
        await _stop_process(process, cid_file)
        await _cancel_tasks(tasks)
        return ContainerResult(returncode=-1, stdout=b"", stderr=b"", output_limited=True)
    except asyncio.CancelledError:
        await _stop_process(process, cid_file)
        await _cancel_tasks(tasks)
        raise
    finally:
        try:
            cid_file.unlink(missing_ok=True)
        except OSError:
            pass


async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _container_identity() -> tuple[int, int]:
    uid = os.geteuid() if hasattr(os, "geteuid") else 65534
    gid = os.getegid() if hasattr(os, "getegid") else 65534
    if uid == 0:
        uid, gid = 65534, 65534
    return uid, gid


def _set_owner(path: Path, uid: int, gid: int) -> None:
    if hasattr(os, "chown"):
        try:
            os.chown(path, uid, gid)
        except PermissionError:
            pass


def _is_docker_error(output: bytes) -> bool:
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in (
            b"docker: error",
            b"error response from daemon",
            b"cannot connect to the docker daemon",
            b"error during connect:",
        )
    )


def _short_message(output: bytes) -> str:
    return output.decode("utf-8", errors="replace").strip()[:4000] or "No compiler output."

