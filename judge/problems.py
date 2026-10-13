"""Load and validate bundled problem packages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROBLEM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class TestCase:
    input: str
    output: str
    points: int


@dataclass(frozen=True)
class Problem:
    id: str
    title: str
    statement: str
    sample_input: str
    sample_output: str
    time_limit_ms: int
    memory_limit_mb: int
    compile_timeout_sec: int
    tests: tuple[TestCase, ...]

    def public_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "statement": self.statement,
            "sample_input": self.sample_input,
            "sample_output": self.sample_output,
            "time_limit_ms": self.time_limit_ms,
            "memory_limit_mb": self.memory_limit_mb,
            "language": "cpp17",
            "test_count": len(self.tests),
            "total_points": sum(test.points for test in self.tests),
        }


class ProblemSet:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self._problems: dict[str, Problem] = {}
        self._load()

    def _load(self) -> None:
        if not self.directory.is_dir():
            raise ValueError(f"Problem directory not found: {self.directory}")
        for path in sorted(self.directory.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            problem = self._parse(raw, path)
            if problem.id in self._problems:
                raise ValueError(f"Duplicate problem id: {problem.id}")
            self._problems[problem.id] = problem
        if not self._problems:
            raise ValueError(f"No problem manifests found in {self.directory}")

    @staticmethod
    def _parse(raw: Any, path: Path) -> Problem:
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name}: manifest must be an object")
        required = ("id", "title", "statement", "sample_input", "sample_output", "tests")
        if any(key not in raw for key in required):
            raise ValueError(f"{path.name}: missing required manifest field")
        problem_id = raw["id"]
        if not isinstance(problem_id, str) or not PROBLEM_ID.fullmatch(problem_id):
            raise ValueError(f"{path.name}: invalid problem id")
        if not isinstance(raw["title"], str) or not raw["title"].strip():
            raise ValueError(f"{path.name}: title must be non-empty")
        if not isinstance(raw["statement"], str) or not raw["statement"].strip():
            raise ValueError(f"{path.name}: statement must be non-empty")
        tests_raw = raw["tests"]
        if not isinstance(tests_raw, list) or not tests_raw:
            raise ValueError(f"{path.name}: tests must be a non-empty list")
        tests: list[TestCase] = []
        for index, test in enumerate(tests_raw, start=1):
            if not isinstance(test, dict):
                raise ValueError(f"{path.name}: test {index} must be an object")
            if not isinstance(test.get("input"), str) or not isinstance(test.get("output"), str):
                raise ValueError(f"{path.name}: test {index} needs string input and output")
            points = test.get("points")
            if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
                raise ValueError(f"{path.name}: test {index} points must be a positive integer")
            tests.append(TestCase(test["input"], test["output"], points))
        if sum(test.points for test in tests) != 100:
            raise ValueError(f"{path.name}: test weights must sum to 100")
        time_limit = raw.get("time_limit_ms", 2000)
        memory_limit = raw.get("memory_limit_mb", 256)
        compile_timeout = raw.get("compile_timeout_sec", 15)
        if isinstance(time_limit, bool) or not isinstance(time_limit, int) or not 100 <= time_limit <= 30000:
            raise ValueError(f"{path.name}: time_limit_ms must be between 100 and 30000")
        if isinstance(memory_limit, bool) or not isinstance(memory_limit, int) or not 16 <= memory_limit <= 4096:
            raise ValueError(f"{path.name}: memory_limit_mb must be between 16 and 4096")
        if isinstance(compile_timeout, bool) or not isinstance(compile_timeout, int) or not 1 <= compile_timeout <= 120:
            raise ValueError(f"{path.name}: compile_timeout_sec must be between 1 and 120")
        for key in ("sample_input", "sample_output"):
            if not isinstance(raw[key], str):
                raise ValueError(f"{path.name}: {key} must be a string")
        return Problem(
            id=problem_id,
            title=raw["title"].strip(),
            statement=raw["statement"],
            sample_input=raw["sample_input"],
            sample_output=raw["sample_output"],
            time_limit_ms=time_limit,
            memory_limit_mb=memory_limit,
            compile_timeout_sec=compile_timeout,
            tests=tuple(tests),
        )

    def get(self, problem_id: str) -> Problem | None:
        return self._problems.get(problem_id)

    def list_public(self) -> list[dict[str, Any]]:
        return [
            {
                "id": problem.id,
                "title": problem.title,
                "time_limit_ms": problem.time_limit_ms,
                "memory_limit_mb": problem.memory_limit_mb,
                "test_count": len(problem.tests),
                "total_points": sum(test.points for test in problem.tests),
            }
            for problem in sorted(self._problems.values(), key=lambda item: item.id)
        ]

