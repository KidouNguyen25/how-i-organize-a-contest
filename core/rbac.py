"""A01: Role-Based Access Control (RBAC), Scope Enforcement, and Leak Prevention Engine."""

from __future__ import annotations

from typing import Any, Optional
from core.models import Role


class SecurityViolation(PermissionError):
    """Raised when an unauthorized actor attempts an illegal operation or data access."""
    pass


class DualApprovalRequired(PermissionError):
    """Raised when an operation requires dual-approval by two distinct authorized users."""
    pass


class RBACContext:
    def __init__(self, user_id: str, role: Role, assigned_season_id: Optional[str] = None) -> None:
        self.user_id = user_id
        self.role = role
        self.assigned_season_id = assigned_season_id

    def is_operator(self) -> bool:
        return self.role == Role.SYSTEM_OPERATOR

    def is_organizer(self, season_id: str) -> bool:
        return self.is_operator() or (self.role == Role.ORGANIZER and self.assigned_season_id == season_id)

    def is_arbiter(self, season_id: str) -> bool:
        return self.is_operator() or (self.role == Role.ARBITER and self.assigned_season_id == season_id)

    def is_auditor(self) -> bool:
        return self.role == Role.AUDITOR or self.is_operator()


class ScopeEnforcer:
    """Enforces fine-grained scope permissions, IDOR defense, and confidential data filtering."""

    @staticmethod
    def check_submission_access(ctx: RBACContext, season_id: str, submission_owner_user_id: str) -> None:
        """Contestants can only view their own submission source. Organizers/Arbiters can view within season."""
        if ctx.is_organizer(season_id) or ctx.is_arbiter(season_id) or ctx.is_auditor():
            return
        if ctx.role == Role.CONTESTANT and ctx.user_id == submission_owner_user_id:
            return
        raise SecurityViolation(f"IDOR violation: User {ctx.user_id} denied access to submission of user {submission_owner_user_id}")

    @staticmethod
    def check_season_management(ctx: RBACContext, season_id: str) -> None:
        """Only assigned Organizer or System Operator can manage a season."""
        if not ctx.is_organizer(season_id):
            raise SecurityViolation(f"User {ctx.user_id} lacks ORGANIZER permissions for season {season_id}")

    @staticmethod
    def check_dual_approval(
        initiator_id: str,
        initiator_role: Role,
        approver_id: str,
        approver_role: Role,
        season_id: str
    ) -> None:
        """Verifies dual-approval policy: Initiator and Approver must be two distinct users with valid roles."""
        if initiator_id == approver_id:
            raise DualApprovalRequired("Dual-approval rejected: Initiator and Approver cannot be the same person.")
        valid_roles = {Role.ORGANIZER, Role.ARBITER, Role.SYSTEM_OPERATOR}
        if initiator_role not in valid_roles or approver_role not in valid_roles:
            raise SecurityViolation("Dual-approval rejected: Both actors must hold ORGANIZER, ARBITER, or OPERATOR roles.")

    @staticmethod
    def sanitize_placement_observation(observation: dict[str, Any], actor_side: str, game_phase: str) -> dict[str, Any]:
        """In OTT placement phase, hide opponent's placed pieces. Reveal only when phase transitions to PLAY."""
        if game_phase == "PLAY" or game_phase == "FINISHED":
            return observation  # Both sides revealed
        
        # In PLACEMENT phase:
        sanitized = dict(observation)
        if "pieces" in sanitized:
            sanitized["pieces"] = [
                p for p in sanitized["pieces"] if p.get("side") == actor_side
            ]
        sanitized["opponent_placement_hidden"] = True
        return sanitized

    @staticmethod
    def sanitize_test_suite_evaluation(result: dict[str, Any], ctx: RBACContext) -> dict[str, Any]:
        """Spectators and regular contestants receive verdict, execution time, and points. Secret input/output are purged."""
        if ctx.is_arbiter(result.get("season_id", "")) or ctx.is_operator():
            return result  # Arbiters can view secret test inputs
        
        sanitized = dict(result)
        if "test_results" in sanitized:
            cleaned_tests = []
            for t in sanitized["test_results"]:
                t_copy = dict(t)
                t_copy.pop("secret_input", None)
                t_copy.pop("expected_output", None)
                t_copy.pop("program_output", None)
                cleaned_tests.append(t_copy)
            sanitized["test_results"] = cleaned_tests
        return sanitized
