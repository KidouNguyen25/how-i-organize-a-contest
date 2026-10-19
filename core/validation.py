"""A02: Configuration and Snapshot Validation Engine."""

from __future__ import annotations

from typing import Any
from core.models import CompetitionDefinition, SeasonSnapshot


class ValidationError(ValueError):
    """Raised when a configuration or snapshot violates validation invariants."""
    pass


class ConfigurationValidator:
    """Validates CompetitionDefinitions and SeasonSnapshots to guarantee safety and consistency."""

    @staticmethod
    def validate_competition_definition(defn: CompetitionDefinition) -> None:
        if not defn.definition_id or len(defn.definition_id) < 3:
            raise ValidationError("definition_id must be at least 3 characters long.")
        if not defn.supported_adapters:
            raise ValidationError("supported_adapters list cannot be empty.")
        if not defn.supported_runtimes:
            raise ValidationError("supported_runtimes list cannot be empty.")

    @staticmethod
    def validate_season_snapshot(snapshot: SeasonSnapshot, comp_def: CompetitionDefinition) -> None:
        # 1. Check definition binding
        if snapshot.competition_definition_id != comp_def.definition_id:
            raise ValidationError(
                f"Snapshot references definition '{snapshot.competition_definition_id}' but validated against '{comp_def.definition_id}'"
            )

        # 2. Check runtime compatibility
        if snapshot.runtime_profile.profile_id not in comp_def.supported_runtimes:
            raise ValidationError(
                f"Runtime profile '{snapshot.runtime_profile.profile_id}' is not in supported runtimes: {comp_def.supported_runtimes}"
            )

        # 3. Check time windows
        quotas = snapshot.limits_and_quotas
        if quotas.submission_window_closes_at <= quotas.submission_window_opens_at:
            raise ValidationError("submission_window_closes_at must be strictly greater than opens_at.")

        # 4. Check limits and resource sanity
        if quotas.max_teams < 2:
            raise ValidationError("max_teams must be at least 2.")
        if quotas.max_source_size_bytes > 1048576:  # 1 MiB limit
            raise ValidationError("max_source_size_bytes exceeds maximum allowed platform cap of 1 MiB.")
        if snapshot.runtime_profile.compile_timeout_sec <= 0:
            raise ValidationError("compile_timeout_sec must be positive.")
        if snapshot.runtime_profile.compile_memory_limit_mb < 128:
            raise ValidationError("compile_memory_limit_mb must be at least 128 MiB.")

        # 5. Check tie-breaker completeness (Must be non-empty and end deterministically)
        tie_breakers = snapshot.scoring_parameters.get("tie_breakers", [])
        if not tie_breakers:
            raise ValidationError("scoring_parameters must define at least one tie_breaker.")
        deterministic_breakers = {"REGISTRATION_TIME", "ENTRY_ID", "SUBMISSION_ID", "SUBMISSION_TIME"}
        if tie_breakers[-1] not in deterministic_breakers:
            raise ValidationError(
                f"The final tie_breaker must be fully deterministic (one of {deterministic_breakers}), got: '{tie_breakers[-1]}'"
            )

        # 6. Check category vs scoring/format compatibility
        if comp_def.category == "INTERACTIVE_BOT_MATCH":
            if snapshot.format_type not in {"DOUBLE_ROUND_ROBIN", "SINGLE_ELIMINATION", "SWISS"}:
                raise ValidationError(f"Incompatible format '{snapshot.format_type}' for bot match arena.")
            if snapshot.scoring_policy_type != "BOT_MATCH_POINTS":
                raise ValidationError(f"Incompatible scoring policy '{snapshot.scoring_policy_type}' for bot arena.")
        elif comp_def.category == "TEST_SUITE_PROBLEM":
            if snapshot.format_type != "DIRECT_TASK_QUEUE":
                raise ValidationError(f"Incompatible format '{snapshot.format_type}' for test-suite problem.")
            if snapshot.scoring_policy_type != "TEST_SUITE_WEIGHTED_SUM":
                raise ValidationError(f"Incompatible scoring policy '{snapshot.scoring_policy_type}' for test-suite.")

        # 7. Check dispute & retention
        if snapshot.dispute_and_retention.protest_window_seconds < 3600:
            raise ValidationError("protest_window_seconds must be at least 1 hour (3600s).")
        if snapshot.dispute_and_retention.artifact_retention_days < 7:
            raise ValidationError("artifact_retention_days must be at least 7 days.")
