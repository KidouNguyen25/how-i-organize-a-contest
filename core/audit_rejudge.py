"""A07: Audit Log Engine, Dual-Approval Rejudge Coordinator, and Appeals State Machine."""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional
from core.models import Appeal, AppealStatus, AuditEntry, Role
from core.rbac import DualApprovalRequired, SecurityViolation


class AuditLogManager:
    """Append-only audit log manager tracking sensitive configuration and score changes."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(self, season_id: str, actor_id: str, action_type: str, payload_diff: dict[str, Any]) -> AuditEntry:
        entry = AuditEntry(
            log_id=str(uuid.uuid4()),
            season_id=season_id,
            actor_id=actor_id,
            action_type=action_type,
            payload_diff=payload_diff,
            created_at=int(time.time())
        )
        self._entries.append(entry)
        return entry

    def get_entries(self, season_id: Optional[str] = None) -> list[AuditEntry]:
        if season_id:
            return [e for e in self._entries if e.season_id == season_id]
        return list(self._entries)


class RejudgeCoordinator:
    """Coordinates rejudge batches with mandatory dual-approval and revision tracking."""

    def __init__(self, audit_mgr: AuditLogManager) -> None:
        self.audit_mgr = audit_mgr
        self._pending_requests: dict[str, dict[str, Any]] = {}

    def request_rejudge(
        self,
        request_id: str,
        season_id: str,
        initiator_id: str,
        initiator_role: Role,
        scope: str,
        target_ids: list[str],
        reason: str
    ) -> dict[str, Any]:
        if initiator_role not in {Role.ORGANIZER, Role.ARBITER, Role.SYSTEM_OPERATOR}:
            raise SecurityViolation("Only Organizers, Arbiters, or Operators can request a rejudge.")

        req = {
            "request_id": request_id,
            "season_id": season_id,
            "initiator_id": initiator_id,
            "initiator_role": initiator_role,
            "scope": scope,
            "target_ids": target_ids,
            "reason": reason,
            "status": "PENDING_APPROVAL",
            "requested_at": int(time.time())
        }
        self._pending_requests[request_id] = req
        self.audit_mgr.record(
            season_id=season_id,
            actor_id=initiator_id,
            action_type="REJUDGE_REQUESTED",
            payload_diff={"request_id": request_id, "scope": scope, "reason": reason}
        )
        return req

    def approve_rejudge(
        self,
        request_id: str,
        approver_id: str,
        approver_role: Role
    ) -> dict[str, Any]:
        req = self._pending_requests.get(request_id)
        if not req:
            raise KeyError(f"Rejudge request '{request_id}' not found.")

        # Dual-approval invariant
        if approver_id == req["initiator_id"]:
            raise DualApprovalRequired("Dual-approval rejected: Approver must be distinct from Initiator.")
        if approver_role not in {Role.ORGANIZER, Role.ARBITER, Role.SYSTEM_OPERATOR}:
            raise SecurityViolation("Approver lacks required administrative role.")

        req["status"] = "APPROVED"
        req["approver_id"] = approver_id
        req["approved_at"] = int(time.time())

        self.audit_mgr.record(
            season_id=req["season_id"],
            actor_id=approver_id,
            action_type="REJUDGE_APPROVED",
            payload_diff={"request_id": request_id, "approver_id": approver_id}
        )
        return req


class AppealWorkflowManager:
    """State machine governing contestant protests and arbitrated resolutions."""

    def __init__(self, audit_mgr: AuditLogManager) -> None:
        self.audit_mgr = audit_mgr
        self._appeals: dict[str, Appeal] = {}

    def submit_appeal(
        self,
        season_id: str,
        entry_id: str,
        target_evaluation_id: str,
        reason: str,
        submitted_at: int,
        protest_deadline: int
    ) -> Appeal:
        if submitted_at > protest_deadline:
            raise ValueError(f"Protest window expired at timestamp {protest_deadline} (current: {submitted_at})")

        appeal = Appeal(
            appeal_id=f"appeal-{uuid.uuid4().hex[:8]}",
            season_id=season_id,
            entry_id=entry_id,
            target_evaluation_id=target_evaluation_id,
            reason=reason,
            submitted_at=submitted_at,
            status=AppealStatus.SUBMITTED
        )
        self._appeals[appeal.appeal_id] = appeal
        self.audit_mgr.record(
            season_id=season_id,
            actor_id=entry_id,
            action_type="APPEAL_SUBMITTED",
            payload_diff={"appeal_id": appeal.appeal_id, "target_evaluation_id": target_evaluation_id}
        )
        return appeal

    def resolve_appeal(
        self,
        appeal_id: str,
        arbiter_id: str,
        arbiter_role: Role,
        accepted: bool,
        resolution_note: str
    ) -> Appeal:
        if arbiter_role not in {Role.ARBITER, Role.ORGANIZER, Role.SYSTEM_OPERATOR}:
            raise SecurityViolation("Only Arbiters or Organizers can resolve appeals.")

        appeal = self._appeals.get(appeal_id)
        if not appeal:
            raise KeyError(f"Appeal '{appeal_id}' not found.")

        appeal.arbiter_id = arbiter_id
        appeal.resolution_note = resolution_note
        appeal.resolved_at = int(time.time())
        appeal.status = AppealStatus.ACCEPTED_REJUDGE if accepted else AppealStatus.REJECTED

        self.audit_mgr.record(
            season_id=appeal.season_id,
            actor_id=arbiter_id,
            action_type="APPEAL_RESOLVED",
            payload_diff={"appeal_id": appeal_id, "status": appeal.status.name, "note": resolution_note}
        )
        return appeal
