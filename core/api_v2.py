"""A03: Core API v2 Controller implementing generic tournament operations."""

from __future__ import annotations

import hashlib
import time
from typing import Any, Optional
from core.adapter import AdapterRegistry
from core.audit_rejudge import AuditLogManager, RejudgeCoordinator
from core.models import (
    CompetitionDefinition,
    CompileStatus,
    Entry,
    EvaluationResult,
    EvaluationTask,
    OfficialSubmissionPolicy,
    Role,
    SeasonSnapshot,
    SeasonStatus,
    Standings,
    Submission,
)
from core.rbac import RBACContext, ScopeEnforcer, SecurityViolation
from core.scoring import BaseScorer, BotArenaMatchPointsScorer, CustomMetricScorer, TestSuiteWeightedSumScorer
from core.validation import ConfigurationValidator


class CoreAPIv2Controller:
    """Controller backing the /api/v2 Generic Contest Platform endpoints."""

    def __init__(self, registry: AdapterRegistry, audit_mgr: AuditLogManager) -> None:
        self.registry = registry
        self.audit_mgr = audit_mgr
        self.rejudge_coord = RejudgeCoordinator(audit_mgr)

        self._definitions: dict[str, CompetitionDefinition] = {}
        self._seasons: dict[str, dict[str, Any]] = {}  # season_id -> {status, snapshot, revision, ...}
        self._entries: dict[str, list[Entry]] = {}     # season_id -> list[Entry]
        self._submissions: dict[str, list[Submission]] = {}  # season_id -> list[Submission]
        self._results: dict[str, list[EvaluationResult]] = {}  # season_id -> list[EvaluationResult]
        self._standings: dict[str, Standings] = {}

    def register_competition_definition(self, defn: CompetitionDefinition) -> None:
        ConfigurationValidator.validate_competition_definition(defn)
        self._definitions[defn.definition_id] = defn

    def create_season(self, season_id: str, name: str, comp_def_id: str, creator_ctx: RBACContext) -> dict[str, Any]:
        if not (creator_ctx.is_operator() or creator_ctx.role == Role.ORGANIZER):
            raise SecurityViolation("Only Organizers or Operators can create contest seasons.")
        if comp_def_id not in self._definitions:
            raise KeyError(f"Competition definition '{comp_def_id}' not found.")

        season_data = {
            "season_id": season_id,
            "name": name,
            "competition_definition_id": comp_def_id,
            "status": SeasonStatus.DRAFT,
            "revision": 1,
            "snapshot": None,
            "created_by": creator_ctx.user_id,
            "created_at": int(time.time())
        }
        self._seasons[season_id] = season_data
        self._entries[season_id] = []
        self._submissions[season_id] = []
        self._results[season_id] = []
        return season_data

    def publish_season_snapshot(self, season_id: str, snapshot: SeasonSnapshot, ctx: RBACContext) -> SeasonSnapshot:
        ScopeEnforcer.check_season_management(ctx, season_id)
        comp_def = self._definitions[snapshot.competition_definition_id]
        ConfigurationValidator.validate_season_snapshot(snapshot, comp_def)

        # Ensure adapter exists in registry
        self.registry.get(snapshot.adapter_id, snapshot.adapter_version)

        season = self._seasons[season_id]
        season["snapshot"] = snapshot
        season["status"] = SeasonStatus.REGISTRATION_OPEN
        season["revision"] = snapshot.revision

        self.audit_mgr.record(
            season_id=season_id,
            actor_id=ctx.user_id,
            action_type="SEASON_PUBLISHED",
            payload_diff={"snapshot_hash": snapshot.compute_hash(), "revision": snapshot.revision}
        )
        return snapshot

    def register_entry(self, season_id: str, team_name: str, ctx: RBACContext) -> Entry:
        season = self._seasons[season_id]
        if season["status"] != SeasonStatus.REGISTRATION_OPEN:
            raise ValueError(f"Cannot register entry: season status is {season['status'].name}")

        snapshot: SeasonSnapshot = season["snapshot"]
        entries = self._entries[season_id]
        if len(entries) >= snapshot.limits_and_quotas.max_teams:
            raise ValueError(f"Team registration quota reached ({snapshot.limits_and_quotas.max_teams} teams).")

        # Check duplicate team name or owner
        if any(e.team_name == team_name for e in entries):
            raise ValueError(f"Team name '{team_name}' is already registered.")

        entry = Entry(
            entry_id=f"team-{len(entries) + 1}",
            season_id=season_id,
            team_name=team_name,
            owner_user_id=ctx.user_id,
            registered_at=int(time.time())
        )
        entries.append(entry)
        return entry

    def submit_code(
        self,
        season_id: str,
        entry_id: str,
        source: str,
        language: str,
        ctx: RBACContext,
        server_receipt_time: int
    ) -> Submission:
        season = self._seasons[season_id]
        snapshot: SeasonSnapshot = season["snapshot"]
        quotas = snapshot.limits_and_quotas

        # Check submission window against official server receipt time
        if server_receipt_time > quotas.submission_window_closes_at:
            raise ValueError("Submission rejected: Deadline has passed.")
        if server_receipt_time < quotas.submission_window_opens_at:
            raise ValueError("Submission rejected: Submission window has not opened.")

        # Check source size
        source_bytes = source.encode("utf-8")
        if len(source_bytes) > quotas.max_source_size_bytes:
            raise ValueError(f"Source size ({len(source_bytes)} B) exceeds platform limit ({quotas.max_source_size_bytes} B).")

        entry = next((e for e in self._entries[season_id] if e.entry_id == entry_id), None)
        if not entry:
            raise KeyError(f"Entry '{entry_id}' not found.")

        # IDOR check
        if ctx.role == Role.CONTESTANT and entry.owner_user_id != ctx.user_id:
            raise SecurityViolation("Cannot submit code for another team.")

        # Quota check on compiles
        if entry.compile_count >= quotas.max_compiles_per_team:
            raise ValueError(f"Compile quota exceeded ({quotas.max_compiles_per_team} compiles allowed).")

        source_hash = hashlib.sha256(source_bytes).hexdigest()
        submission = Submission(
            submission_id=f"sub-{hashlib.sha256(f'{season_id}:{entry_id}:{server_receipt_time}'.encode()).hexdigest()[:12]}",
            season_id=season_id,
            entry_id=entry_id,
            user_id=ctx.user_id,
            language=language,
            source_bytes=source_bytes,
            source_sha256=source_hash,
            received_at=server_receipt_time,
            compile_status=CompileStatus.READY  # Simulating ready for testing
        )

        entry.compile_count += 1
        self._submissions[season_id].append(submission)

        # Apply official submission selection policy
        if snapshot.official_submission_policy == OfficialSubmissionPolicy.LAST_VALID_BEFORE_DEADLINE:
            entry.selected_submission_id = submission.submission_id
            submission.is_official = True
        elif snapshot.official_submission_policy == OfficialSubmissionPolicy.BEST_SCORE:
            if entry.selected_submission_id is None:
                entry.selected_submission_id = submission.submission_id
                submission.is_official = True

        return submission

    def dispatch_evaluations(self, season_id: str, ctx: RBACContext) -> list[EvaluationTask]:
        ScopeEnforcer.check_season_management(ctx, season_id)
        season = self._seasons[season_id]
        snapshot: SeasonSnapshot = season["snapshot"]
        adapter = self.registry.get(snapshot.adapter_id, snapshot.adapter_version)

        entries = self._entries[season_id]
        tasks = adapter.prepare_tasks(snapshot, entries)
        return tasks

    def record_evaluation_result(self, season_id: str, result: EvaluationResult) -> None:
        self._results[season_id].append(result)

    def recalculate_standings(self, season_id: str, ctx: RBACContext) -> Standings:
        season = self._seasons[season_id]
        snapshot: SeasonSnapshot = season["snapshot"]
        entries = self._entries[season_id]
        results = self._results[season_id]

        scorer: BaseScorer
        if snapshot.scoring_policy_type == "TEST_SUITE_WEIGHTED_SUM":
            scorer = TestSuiteWeightedSumScorer()
        elif snapshot.scoring_policy_type == "BOT_MATCH_POINTS":
            scorer = BotArenaMatchPointsScorer()
        else:
            scorer = CustomMetricScorer()

        curr_revision = self._standings.get(season_id).standings_revision + 1 if season_id in self._standings else 1
        standings = scorer.calculate_standings(
            season_id=season_id,
            entries=entries,
            results=results,
            config=snapshot.scoring_parameters,
            revision=curr_revision,
            timestamp=int(time.time())
        )
        self._standings[season_id] = standings
        return standings

    def get_public_standings(self, season_id: str, ctx: RBACContext) -> Standings:
        if season_id not in self._standings:
            self.recalculate_standings(season_id, ctx)
        return self._standings[season_id]
