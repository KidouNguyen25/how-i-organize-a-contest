"""Bot Arena Adapter for turn-based games (OTT Arena v1)."""

from __future__ import annotations

from typing import Any
from core.adapter import BaseEvaluationAdapter
from core.formats import DoubleRoundRobinGenerator
from core.models import Entry, EvaluationResult, EvaluationStatus, EvaluationTask, SeasonSnapshot, Verdict


class BotAdapter(BaseEvaluationAdapter):
    """Adapter for interactive turn-based bot matches (OTT Arena)."""

    @property
    def adapter_id(self) -> str:
        return "ott-bot-engine"

    @property
    def adapter_version(self) -> str:
        return "1.0.0"

    def validate_season_config(self, config: dict[str, Any]) -> tuple[bool, str]:
        clock = config.get("clock_seconds", 300)
        if clock not in {300, 600, 3600}:
            return False, f"clock_seconds must be 300, 600, or 3600, got: {clock}"
        return True, "Valid"

    def prepare_tasks(self, snapshot: SeasonSnapshot, entries: list[Entry]) -> list[EvaluationTask]:
        generator = DoubleRoundRobinGenerator()
        return generator.generate_tasks(snapshot, entries)

    def parse_raw_output(self, task: EvaluationTask, raw_worker_output: dict[str, Any]) -> EvaluationResult:
        status_str = raw_worker_output.get("status", "SUCCEEDED")
        verdict_str = raw_worker_output.get("result", "DRAW")
        moves = int(raw_worker_output.get("move_count", 0))
        duration = int(raw_worker_output.get("duration_ms", 0))

        status_enum = EvaluationStatus[status_str] if status_str in EvaluationStatus.__members__ else EvaluationStatus.SUCCEEDED
        verdict_enum = Verdict[verdict_str] if verdict_str in Verdict.__members__ else Verdict.DRAW

        return EvaluationResult(
            task_id=task.task_id,
            result_revision=raw_worker_output.get("revision", 1),
            status=status_enum,
            verdict=verdict_enum,
            raw_metrics={
                "white_entry_id": task.task_payload.get("white_entry_id"),
                "black_entry_id": task.task_payload.get("black_entry_id"),
                "move_count": moves,
                "winner_side": raw_worker_output.get("winner_side")
            },
            resource_usage={
                "cpu_time_ms": int(raw_worker_output.get("cpu_time_ms", 0)),
                "max_memory_kb": int(raw_worker_output.get("max_memory_kb", 0))
            },
            execution_duration_ms=duration,
            error_message=raw_worker_output.get("reason_code")
        )

    def filter_public_view(self, detailed_state: dict[str, Any], audience_role: str) -> dict[str, Any]:
        """In PLACEMENT phase, hide opponent's pieces unless game has transitioned to PLAY."""
        filtered = dict(detailed_state)
        filtered.pop("secret_test_data", None)
        phase = filtered.get("phase", "PLAY")
        if phase in {"PLAY", "FINISHED"}:
            return filtered

        # Hide unrevealed placement
        if "board_state" in filtered:
            filtered["board_state"] = "[PLACEMENT_HIDDEN_BEFORE_PLAY]"
        return filtered
