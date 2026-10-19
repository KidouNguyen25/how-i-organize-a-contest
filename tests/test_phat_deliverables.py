"""Comprehensive Unit and Integration Test Suite for Phat's Deliverables (A01 - A08)."""

from __future__ import annotations

import unittest
from core.adapter import AdapterConformanceSuite, AdapterRegistry
from core.adapters.bot_adapter import BotAdapter
from core.adapters.test_suite_adapter import TestSuiteAdapter
from core.api_v2 import CoreAPIv2Controller
from core.audit_rejudge import AuditLogManager, RejudgeCoordinator
from core.formats import DoubleRoundRobinGenerator
from core.models import (
    CompetitionDefinition,
    DisputeAndRetention,
    Entry,
    EvaluationResult,
    EvaluationStatus,
    LimitsAndQuotas,
    OfficialSubmissionPolicy,
    Role,
    RuntimeProfile,
    SeasonSnapshot,
    Verdict,
)
from core.rbac import DualApprovalRequired, RBACContext, ScopeEnforcer, SecurityViolation
from core.scoring import BotArenaMatchPointsScorer, TestSuiteWeightedSumScorer
from core.validation import ConfigurationValidator, ValidationError


class TestPhatDeliverables(unittest.TestCase):

    def setUp(self) -> None:
        self.registry = AdapterRegistry()
        self.test_suite_adapter = TestSuiteAdapter()
        self.bot_adapter = BotAdapter()
        self.registry.register(self.test_suite_adapter)
        self.registry.register(self.bot_adapter)

        self.audit_mgr = AuditLogManager()
        self.controller = CoreAPIv2Controller(self.registry, self.audit_mgr)

        # Contexts for testing RBAC
        self.ctx_operator = RBACContext(user_id="u_admin", role=Role.SYSTEM_OPERATOR)
        self.ctx_organizer = RBACContext(user_id="u_org", role=Role.ORGANIZER, assigned_season_id="s1")
        self.ctx_arbiter = RBACContext(user_id="u_arb", role=Role.ARBITER, assigned_season_id="s1")
        self.ctx_alice = RBACContext(user_id="u_alice", role=Role.CONTESTANT, assigned_season_id="s1")
        self.ctx_bob = RBACContext(user_id="u_bob", role=Role.CONTESTANT, assigned_season_id="s1")
        self.ctx_spectator = RBACContext(user_id="u_viewer", role=Role.SPECTATOR)

    # -------------------------------------------------------------------------
    # A01: Scope, RBAC, IDOR defense & Leak Prevention
    # -------------------------------------------------------------------------
    def test_a01_idor_defense_and_scope(self) -> None:
        # Contestant accessing their own submission: OK
        ScopeEnforcer.check_submission_access(self.ctx_alice, season_id="s1", submission_owner_user_id="u_alice")

        # Contestant Bob trying to access Alice's submission: Rejected with SecurityViolation
        with self.assertRaises(SecurityViolation):
            ScopeEnforcer.check_submission_access(self.ctx_bob, season_id="s1", submission_owner_user_id="u_alice")

        # Organizer of s1 accessing Alice's submission: OK
        ScopeEnforcer.check_submission_access(self.ctx_organizer, season_id="s1", submission_owner_user_id="u_alice")

    def test_a01_dual_approval_enforcement(self) -> None:
        # Self-approval rejected
        with self.assertRaises(DualApprovalRequired):
            ScopeEnforcer.check_dual_approval(
                initiator_id="u_org",
                initiator_role=Role.ORGANIZER,
                approver_id="u_org",
                approver_role=Role.ORGANIZER,
                season_id="s1"
            )

        # Dual approval by two distinct authorized users: OK
        ScopeEnforcer.check_dual_approval(
            initiator_id="u_org",
            initiator_role=Role.ORGANIZER,
            approver_id="u_arb",
            approver_role=Role.ARBITER,
            season_id="s1"
        )

    def test_a01_leak_prevention(self) -> None:
        # OTT Placement phase hides opponent pieces
        raw_obs = {
            "phase": "PLACEMENT",
            "pieces": [
                {"side": "WHITE", "x": 0, "y": 0, "type": "ROCK"},
                {"side": "BLACK", "x": 8, "y": 8, "type": "SCISSORS"}
            ]
        }
        white_view = ScopeEnforcer.sanitize_placement_observation(raw_obs, actor_side="WHITE", game_phase="PLACEMENT")
        self.assertEqual(len(white_view["pieces"]), 1)
        self.assertEqual(white_view["pieces"][0]["side"], "WHITE")
        self.assertTrue(white_view["opponent_placement_hidden"])

        # When game transitions to PLAY, both sides are revealed
        play_view = ScopeEnforcer.sanitize_placement_observation(raw_obs, actor_side="WHITE", game_phase="PLAY")
        self.assertEqual(len(play_view["pieces"]), 2)

    # -------------------------------------------------------------------------
    # A02: Snapshot Validation and Invariants
    # -------------------------------------------------------------------------
    def test_a02_snapshot_validation_rejects_invalids(self) -> None:
        comp_def = CompetitionDefinition(
            definition_id="classic-algo",
            version="1.0.0",
            category="TEST_SUITE_PROBLEM",
            name="Classic Algo",
            supported_adapters=[{"adapter_id": "test-suite-cpp17", "version_range": "1.0.0"}],
            supported_runtimes=["cpp17-standard"]
        )

        # 1. Invalid time window (closes before opens)
        invalid_snapshot = SeasonSnapshot(
            season_id="s_bad",
            name="Bad Season",
            revision=1,
            competition_definition_id="classic-algo",
            competition_version="1.0.0",
            adapter_id="test-suite-cpp17",
            adapter_version="1.0.0",
            runtime_profile=RuntimeProfile(
                profile_id="cpp17-standard",
                image_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111",
                compile_timeout_sec=15,
                compile_cpu_limit=1.0,
                compile_memory_limit_mb=1024,
                runtime_cpu_limit=1.0,
                runtime_memory_limit_mb=256,
                runtime_pids_limit=64
            ),
            format_type="DIRECT_TASK_QUEUE",
            format_parameters={},
            scoring_policy_type="TEST_SUITE_WEIGHTED_SUM",
            scoring_parameters={"tie_breakers": ["TOTAL_SCORE", "ENTRY_ID"]},
            official_submission_policy=OfficialSubmissionPolicy.BEST_SCORE,
            limits_and_quotas=LimitsAndQuotas(
                max_teams=10,
                max_members_per_team=1,
                max_source_size_bytes=262144,
                max_compiles_per_team=10,
                submission_window_opens_at=2000,
                submission_window_closes_at=1000  # Invalid! Closes before opens
            ),
            dispute_and_retention=DisputeAndRetention()
        )
        with self.assertRaises(ValidationError) as cm:
            ConfigurationValidator.validate_season_snapshot(invalid_snapshot, comp_def)
        self.assertIn("submission_window_closes_at", str(cm.exception))

    # -------------------------------------------------------------------------
    # A04: Scoring Policies & Deterministic Tie-breaks
    # -------------------------------------------------------------------------
    def test_a04_test_suite_scorer_tiebreak(self) -> None:
        scorer = TestSuiteWeightedSumScorer()
        entries = [
            Entry(entry_id="team_1", season_id="s1", team_name="Alpha", owner_user_id="u1"),
            Entry(entry_id="team_2", season_id="s1", team_name="Beta", owner_user_id="u2")
        ]
        # Both get 100 points, but team_1 used 15ms CPU, team_2 used 30ms CPU
        results = [
            EvaluationResult(
                task_id="t1",
                result_revision=1,
                status=EvaluationStatus.SUCCEEDED,
                verdict=Verdict.AC,
                raw_metrics={"entry_id": "team_1", "score": 100.0, "received_at": 100},
                resource_usage={"cpu_time_ms": 15, "max_memory_kb": 1024},
                execution_duration_ms=50
            ),
            EvaluationResult(
                task_id="t2",
                result_revision=1,
                status=EvaluationStatus.SUCCEEDED,
                verdict=Verdict.AC,
                raw_metrics={"entry_id": "team_2", "score": 100.0, "received_at": 105},
                resource_usage={"cpu_time_ms": 30, "max_memory_kb": 1024},
                execution_duration_ms=60
            )
        ]
        standings = scorer.calculate_standings("s1", entries, results, {}, revision=1, timestamp=500)
        self.assertEqual(standings.rows[0].entry_id, "team_1")
        self.assertEqual(standings.rows[0].rank, 1)
        self.assertEqual(standings.rows[1].entry_id, "team_2")
        self.assertEqual(standings.rows[1].rank, 2)

    def test_a04_bot_arena_head_to_head_tiebreak(self) -> None:
        scorer = BotArenaMatchPointsScorer()
        entries = [
            Entry(entry_id="bot_a", season_id="s1", team_name="Alpha", owner_user_id="u1", registered_at=10),
            Entry(entry_id="bot_b", season_id="s1", team_name="Beta", owner_user_id="u2", registered_at=20)
        ]
        # Match 1: bot_a wins against bot_b (3 pts vs 0 pts)
        # Match 2: bot_b wins against bot_a (3 pts vs 0 pts) -> Tied on points (3 each), tied on H2H (3 each)
        results = [
            EvaluationResult(
                task_id="m1",
                result_revision=1,
                status=EvaluationStatus.SUCCEEDED,
                verdict=Verdict.WHITE_WIN,
                raw_metrics={"white_entry_id": "bot_a", "black_entry_id": "bot_b"},
                resource_usage={"cpu_time_ms": 100, "max_memory_kb": 2048},
                execution_duration_ms=500
            ),
            EvaluationResult(
                task_id="m2",
                result_revision=1,
                status=EvaluationStatus.SUCCEEDED,
                verdict=Verdict.WHITE_WIN,
                raw_metrics={"white_entry_id": "bot_b", "black_entry_id": "bot_a"},
                resource_usage={"cpu_time_ms": 100, "max_memory_kb": 2048},
                execution_duration_ms=500
            )
        ]
        standings = scorer.calculate_standings("s1", entries, results, {"points_win": 3, "points_draw": 1, "points_loss": 0})
        # Tied on points and diff; bot_a registered earlier (10 < 20), so bot_a ranks higher
        self.assertEqual(standings.rows[0].entry_id, "bot_a")
        self.assertEqual(standings.rows[1].entry_id, "bot_b")

    # -------------------------------------------------------------------------
    # A05: Adapter Contract & Conformance Testing
    # -------------------------------------------------------------------------
    def test_a05_adapter_conformance(self) -> None:
        self.assertTrue(AdapterConformanceSuite.verify(self.test_suite_adapter))
        self.assertTrue(AdapterConformanceSuite.verify(self.bot_adapter))

    # -------------------------------------------------------------------------
    # A07: Audit Log, Rejudge & Appeals
    # -------------------------------------------------------------------------
    def test_a07_audit_log_and_rejudge_workflow(self) -> None:
        rejudge_coord = RejudgeCoordinator(self.audit_mgr)

        # 1. Organizer requests rejudge
        req = rejudge_coord.request_rejudge(
            request_id="rj-001",
            season_id="s1",
            initiator_id="u_org",
            initiator_role=Role.ORGANIZER,
            scope="ALL_SEASON",
            target_ids=["all"],
            reason="Corrected test case 3"
        )
        self.assertEqual(req["status"], "PENDING_APPROVAL")

        # 2. Self-approval rejected
        with self.assertRaises(DualApprovalRequired):
            rejudge_coord.approve_rejudge("rj-001", approver_id="u_org", approver_role=Role.ORGANIZER)

        # 3. Arbiter approves rejudge
        approved = rejudge_coord.approve_rejudge("rj-001", approver_id="u_arb", approver_role=Role.ARBITER)
        self.assertEqual(approved["status"], "APPROVED")

        # 4. Check audit log entries
        logs = self.audit_mgr.get_entries(season_id="s1")
        actions = [entry.action_type for entry in logs]
        self.assertIn("REJUDGE_REQUESTED", actions)
        self.assertIn("REJUDGE_APPROVED", actions)

    # -------------------------------------------------------------------------
    # A08: Dual Season End-to-End Verification on Unified Core API
    # -------------------------------------------------------------------------
    def test_a08_dual_seasons_on_unified_core(self) -> None:
        # Register definitions
        def_cp = CompetitionDefinition(
            definition_id="classic-cp",
            version="1.0.0",
            category="TEST_SUITE_PROBLEM",
            name="Classic CP",
            supported_adapters=[{"adapter_id": "test-suite-cpp17", "version_range": "1.0.0"}],
            supported_runtimes=["cpp17-gcc13-linux"]
        )
        def_bot = CompetitionDefinition(
            definition_id="ott-arena",
            version="1.0.0",
            category="INTERACTIVE_BOT_MATCH",
            name="OTT Arena",
            supported_adapters=[{"adapter_id": "ott-bot-engine", "version_range": "1.0.0"}],
            supported_runtimes=["cpp17-gcc13-linux"]
        )
        self.controller.register_competition_definition(def_cp)
        self.controller.register_competition_definition(def_bot)

        # 1. Run Season 1: CP Test Suite
        self.controller.create_season("season_cp", "Freshman CP Cup", "classic-cp", self.ctx_operator)
        snap_cp = SeasonSnapshot(
            season_id="season_cp",
            name="Freshman CP Cup",
            revision=1,
            competition_definition_id="classic-cp",
            competition_version="1.0.0",
            adapter_id="test-suite-cpp17",
            adapter_version="1.0.0",
            runtime_profile=RuntimeProfile(
                profile_id="cpp17-gcc13-linux",
                image_digest="sha256:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
                compile_timeout_sec=15,
                compile_cpu_limit=1.0,
                compile_memory_limit_mb=1024,
                runtime_cpu_limit=1.0,
                runtime_memory_limit_mb=256,
                runtime_pids_limit=64
            ),
            format_type="DIRECT_TASK_QUEUE",
            format_parameters={},
            scoring_policy_type="TEST_SUITE_WEIGHTED_SUM",
            scoring_parameters={"tie_breakers": ["TOTAL_SCORE", "ENTRY_ID"]},
            official_submission_policy=OfficialSubmissionPolicy.BEST_SCORE,
            limits_and_quotas=LimitsAndQuotas(
                max_teams=30,
                max_members_per_team=1,
                max_source_size_bytes=262144,
                max_compiles_per_team=10,
                submission_window_opens_at=1000,
                submission_window_closes_at=5000
            ),
            dispute_and_retention=DisputeAndRetention(),
            adapter_custom_config={"problem_id": "sum-two-integers", "test_cases": [{"case_id": "1", "points": 100}]}
        )
        self.controller.publish_season_snapshot("season_cp", snap_cp, self.ctx_operator)

        # Alice registers and submits code
        entry_alice = self.controller.register_entry("season_cp", "AliceTeam", self.ctx_alice)
        sub_alice = self.controller.submit_code(
            "season_cp", entry_alice.entry_id, "int main(){ return 0; }", "cpp17", self.ctx_alice, server_receipt_time=1500
        )
        self.assertTrue(sub_alice.is_official)

        # Dispatch evaluation tasks
        tasks_cp = self.controller.dispatch_evaluations("season_cp", self.ctx_operator)
        self.assertEqual(len(tasks_cp), 1)

        # Record result and calculate standings
        self.controller.record_evaluation_result("season_cp", EvaluationResult(
            task_id=tasks_cp[0].task_id,
            result_revision=1,
            status=EvaluationStatus.SUCCEEDED,
            verdict=Verdict.AC,
            raw_metrics={"entry_id": entry_alice.entry_id, "score": 100.0, "received_at": 1500},
            resource_usage={"cpu_time_ms": 10, "max_memory_kb": 1024},
            execution_duration_ms=40
        ))
        standings_cp = self.controller.recalculate_standings("season_cp", self.ctx_operator)
        self.assertEqual(len(standings_cp.rows), 1)
        self.assertEqual(standings_cp.rows[0].points, 100.0)

        # 2. Run Season 2: Bot Arena (Double Round Robin with 2 teams)
        self.controller.create_season("season_bot", "OTT Bot Cup", "ott-arena", self.ctx_operator)
        snap_bot = SeasonSnapshot(
            season_id="season_bot",
            name="OTT Bot Cup",
            revision=1,
            competition_definition_id="ott-arena",
            competition_version="1.0.0",
            adapter_id="ott-bot-engine",
            adapter_version="1.0.0",
            runtime_profile=RuntimeProfile(
                profile_id="cpp17-gcc13-linux",
                image_digest="sha256:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
                compile_timeout_sec=15,
                compile_cpu_limit=1.0,
                compile_memory_limit_mb=1024,
                runtime_cpu_limit=1.0,
                runtime_memory_limit_mb=256,
                runtime_pids_limit=64
            ),
            format_type="DOUBLE_ROUND_ROBIN",
            format_parameters={"swap_sides": True},
            scoring_policy_type="BOT_MATCH_POINTS",
            scoring_parameters={"points_win": 3, "points_draw": 1, "points_loss": 0, "tie_breakers": ["HEAD_TO_HEAD", "ENTRY_ID"]},
            official_submission_policy=OfficialSubmissionPolicy.LAST_VALID_BEFORE_DEADLINE,
            limits_and_quotas=LimitsAndQuotas(
                max_teams=30,
                max_members_per_team=3,
                max_source_size_bytes=262144,
                max_compiles_per_team=10,
                submission_window_opens_at=1000,
                submission_window_closes_at=5000
            ),
            dispute_and_retention=DisputeAndRetention(),
            adapter_custom_config={"clock_seconds": 300}
        )
        self.controller.publish_season_snapshot("season_bot", snap_bot, self.ctx_operator)

        e1 = self.controller.register_entry("season_bot", "TeamWhite", self.ctx_alice)
        e2 = self.controller.register_entry("season_bot", "TeamBlack", self.ctx_bob)
        self.controller.submit_code("season_bot", e1.entry_id, "bot_w", "cpp17", self.ctx_alice, server_receipt_time=2000)
        self.controller.submit_code("season_bot", e2.entry_id, "bot_b", "cpp17", self.ctx_bob, server_receipt_time=2100)

        tasks_bot = self.controller.dispatch_evaluations("season_bot", self.ctx_operator)
        # Double round robin for 2 teams yields exactly 2 matches (leg 1 and leg 2 swapped)
        self.assertEqual(len(tasks_bot), 2)


if __name__ == "__main__":
    unittest.main()
