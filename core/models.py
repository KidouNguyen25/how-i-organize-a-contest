"""Domain models, contracts, and data structures for Core Platform v2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class Role(str, Enum):
    SYSTEM_OPERATOR = "SYSTEM_OPERATOR"
    ORGANIZER = "ORGANIZER"
    ARBITER = "ARBITER"
    AUDITOR = "AUDITOR"
    CONTESTANT = "CONTESTANT"
    SPECTATOR = "SPECTATOR"


class SeasonStatus(str, Enum):
    DRAFT = "DRAFT"
    REGISTRATION_OPEN = "REGISTRATION_OPEN"
    LOCKED = "LOCKED"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


class CompileStatus(str, Enum):
    QUEUED = "QUEUED"
    COMPILING = "COMPILING"
    READY = "READY"
    CE = "CE"


class OfficialSubmissionPolicy(str, Enum):
    LAST_VALID_BEFORE_DEADLINE = "LAST_VALID_BEFORE_DEADLINE"
    BEST_SCORE = "BEST_SCORE"
    MANUAL_LOCK = "MANUAL_LOCK"


class EvaluationStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_SUBMISSION = "FAILED_SUBMISSION"
    INFRA_ERROR = "INFRA_ERROR"
    PAUSED_REVIEW = "PAUSED_REVIEW"


class Verdict(str, Enum):
    AC = "AC"
    WA = "WA"
    TLE = "TLE"
    MLE = "MLE"
    OLE = "OLE"
    CE = "CE"
    RE = "RE"
    WHITE_WIN = "WHITE_WIN"
    BLACK_WIN = "BLACK_WIN"
    DRAW = "DRAW"
    SCORE = "SCORE"


class AppealStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    EVIDENCE_COLLECTED = "EVIDENCE_COLLECTED"
    ACCEPTED_REJUDGE = "ACCEPTED_REJUDGE"
    REJECTED = "REJECTED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class RuntimeProfile:
    profile_id: str
    image_digest: str
    compile_timeout_sec: int
    compile_cpu_limit: float
    compile_memory_limit_mb: int
    runtime_cpu_limit: float
    runtime_memory_limit_mb: int
    runtime_pids_limit: int
    network_mode: str = "none"


@dataclass(frozen=True)
class LimitsAndQuotas:
    max_teams: int
    max_members_per_team: int
    max_source_size_bytes: int
    max_compiles_per_team: int
    submission_window_opens_at: int
    submission_window_closes_at: int


@dataclass(frozen=True)
class DisputeAndRetention:
    protest_window_seconds: int = 86400  # 24 hours
    artifact_retention_days: int = 90


@dataclass(frozen=True)
class CompetitionDefinition:
    definition_id: str
    version: str
    category: str
    name: str
    supported_adapters: list[dict[str, str]]
    supported_runtimes: list[str]
    description: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SeasonSnapshot:
    season_id: str
    name: str
    revision: int
    competition_definition_id: str
    competition_version: str
    adapter_id: str
    adapter_version: str
    runtime_profile: RuntimeProfile
    format_type: str
    format_parameters: dict[str, Any]
    scoring_policy_type: str
    scoring_parameters: dict[str, Any]
    official_submission_policy: OfficialSubmissionPolicy
    limits_and_quotas: LimitsAndQuotas
    dispute_and_retention: DisputeAndRetention
    adapter_custom_config: dict[str, Any] = field(default_factory=dict)

    def compute_hash(self) -> str:
        """Compute canonical SHA-256 hash of snapshot configuration."""
        raw_dict = asdict(self)
        raw_dict.pop("snapshot_hash", None)
        canonical_json = json.dumps(raw_dict, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass
class Entry:
    entry_id: str
    season_id: str
    team_name: str
    owner_user_id: str
    member_user_ids: list[str] = field(default_factory=list)
    registered_at: int = 0
    selected_submission_id: Optional[str] = None
    compile_count: int = 0


@dataclass
class Submission:
    submission_id: str
    season_id: str
    entry_id: str
    user_id: str
    language: str
    source_bytes: bytes
    source_sha256: str
    received_at: int  # Server receipt timestamp
    compile_status: CompileStatus = CompileStatus.QUEUED
    compile_message: Optional[str] = None
    binary_sha256: Optional[str] = None
    is_official: bool = False


@dataclass(frozen=True)
class Participant:
    entry_id: str
    submission_id: str
    source_hash: str
    side: str = ""


@dataclass(frozen=True)
class EvaluationTask:
    task_id: str
    season_id: str
    snapshot_hash: str
    adapter_id: str
    adapter_version: str
    attempt_no: int
    participants: list[Participant]
    task_payload: dict[str, Any]
    seed: int = 42
    lease_epoch: int = 1


@dataclass(frozen=True)
class EvaluationResult:
    task_id: str
    result_revision: int
    status: EvaluationStatus
    verdict: Verdict
    raw_metrics: dict[str, Any]
    resource_usage: dict[str, int]
    execution_duration_ms: int
    error_message: Optional[str] = None


@dataclass
class StandingRow:
    rank: int
    entry_id: str
    team_name: str
    points: float
    wins: int = 0
    draws: int = 0
    losses: int = 0
    played: int = 0
    head_to_head_points: float = 0.0
    score_diff: float = 0.0
    penalty_time: int = 0
    tie_break_values: dict[str, Any] = field(default_factory=dict)


@dataclass
class Standings:
    season_id: str
    standings_revision: int
    updated_at: int
    rows: list[StandingRow]


@dataclass
class Appeal:
    appeal_id: str
    season_id: str
    entry_id: str
    target_evaluation_id: str
    reason: str
    submitted_at: int
    status: AppealStatus = AppealStatus.SUBMITTED
    arbiter_id: Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_at: Optional[int] = None


@dataclass(frozen=True)
class AuditEntry:
    log_id: str
    season_id: str
    actor_id: str
    action_type: str
    payload_diff: dict[str, Any]
    created_at: int
