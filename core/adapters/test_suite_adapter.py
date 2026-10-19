"""A06: Test-Suite Adapter integrating classic CP problem judging into Core Platform."""

from __future__ import annotations

from typing import Any
from core.adapter import BaseEvaluationAdapter
from core.formats import DirectTaskQueueGenerator
from core.models import Entry, EvaluationResult, EvaluationStatus, EvaluationTask, SeasonSnapshot, Verdict


class TestSuiteAdapter(BaseEvaluationAdapter):
    """Adapter for testing C++17 submissions against a suite of static test cases."""

    @property
    def adapter_id(self) -> str:
        return "test-suite-cpp17"

    @property
    def adapter_version(self) -> str:
        return "1.0.0"

    def validate_season_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        if "problem_id" not in config:
            return False, "Missing 'problem_id' in adapter_custom_config."
        if "test_cases" not in config:
            return False, "Missing 'test_cases' in adapter_custom_config."
        cases = config.get("test_cases", [])
        if not cases:
            return False, "test_cases list cannot be empty."
        total_points = sum(c.get("points", 0) for c in cases)
        if total_points != 100:
            return False, f"Sum of test_cases points must equal 100, got: {total_points}"
        return True, "Valid"

    def prepare_tasks(self, snapshot: SeasonSnapshot, entries: list[Entry]) -> list[EvaluationTask]:
        generator = DirectTaskQueueGenerator()
        return generator.generate_tasks(snapshot, entries)

    def parse_raw_output(self, task: EvaluationTask, raw_worker_output: dict[str, Any]) -> EvaluationResult:
        """Translates raw container run results into standard EvaluationResult."""
        status_str = raw_worker_output.get("status", "SUCCEEDED")
        verdict_str = raw_worker_output.get("verdict", "WA")
        score = float(raw_worker_output.get("score", 0.0))
        cases = raw_worker_output.get("test_results", [])
        cpu_time = int(raw_worker_output.get("cpu_time_ms", 0))
        max_mem = int(raw_worker_output.get("max_memory_kb", 0))
        duration = int(raw_worker_output.get("duration_ms", 0))
        msg = raw_worker_output.get("message")

        status_enum = EvaluationStatus[status_str] if status_str in EvaluationStatus.__members__ else EvaluationStatus.SUCCEEDED
        verdict_enum = Verdict[verdict_str] if verdict_str in Verdict.__members__ else Verdict.WA

        return EvaluationResult(
            task_id=task.task_id,
            result_revision=raw_worker_output.get("revision", 1),
            status=status_enum,
            verdict=verdict_enum,
            raw_metrics={
                "entry_id": task.task_payload.get("entry_id"),
                "problem_id": task.task_payload.get("problem_id"),
                "score": score,
                "test_results": cases,
                "received_at": raw_worker_output.get("received_at", 0)
            },
            resource_usage={
                "cpu_time_ms": cpu_time,
                "max_memory_kb": max_mem
            },
            execution_duration_ms=duration,
            error_message=msg
        )

    def filter_public_view(self, detailed_state: dict[str, Any], audience_role: str) -> dict[str, Any]:
        """Strip secret inputs and outputs for spectators and normal users."""
        if audience_role in {"ARBITER", "SYSTEM_OPERATOR"}:
            return detailed_state
        
        filtered = dict(detailed_state)
        filtered.pop("secret_test_data", None)
        if "raw_metrics" in filtered and "test_results" in filtered["raw_metrics"]:
            cleaned_tests = []
            for t in filtered["raw_metrics"]["test_results"]:
                t_copy = dict(t)
                t_copy.pop("input", None)
                t_copy.pop("expected_output", None)
                t_copy.pop("actual_output", None)
                cleaned_tests.append(t_copy)
            filtered["raw_metrics"]["test_results"] = cleaned_tests
        return filtered
