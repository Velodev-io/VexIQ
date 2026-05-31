"""Pydantic schemas and data models for VexIQ.

Defines Pydantic models for AIDecision, AIMistake, ProviderProfile, and 
RoutingDecision to structure internal API payloads and SQLite serialization.
"""

from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, field_validator, model_validator


class TaskType(str, Enum):
    """Supported task types in Vexon OS."""

    code_edit = "code_edit"
    command = "command"
    config = "config"
    architecture = "architecture"
    artifact = "artifact"
    other = "other"
    chat = "chat"
    code = "code"


class UserAction(str, Enum):
    """User actions taken on an AI suggestion."""

    accepted = "accepted"
    modified = "modified"
    rejected = "rejected"


class DecisionOutcome(str, Enum):
    """Deferred outcomes of an AI suggestion."""

    kept = "kept"
    reverted = "reverted"
    edited_further = "edited_further"
    unknown = "unknown"


class FailureType(str, Enum):
    """Classifications of AI mistake failure modes."""

    wrong_code = "wrong_code"
    wrong_command = "wrong_command"
    wrong_architecture = "wrong_architecture"
    wrong_config = "wrong_config"
    hallucination = "hallucination"
    incomplete_output = "incomplete_output"
    broken_build = "broken_build"
    test_failure = "test_failure"
    explicit_rejection = "explicit_rejection"
    other = "other"


class Severity(str, Enum):
    """Severity levels of AI mistakes."""

    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class AIDecision(BaseModel):
    """Represents a single decision record of an AI suggestion and its user action."""

    decision_id: str
    session_id: str
    project_id: str | None = None
    task_id: str | None = None
    timestamp: datetime
    provider: str
    model_id: str
    task_type: TaskType
    suggestion_summary: str
    suggestion_hash: str
    user_action: UserAction
    modification_summary: str | None = None
    outcome: DecisionOutcome = DecisionOutcome.unknown
    outcome_recorded_at: datetime | None = None
    confidence_score: float | None = None
    routing_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", "model_id", "session_id", "suggestion_summary", "suggestion_hash")
    @classmethod
    def validate_non_empty_strings(cls, v: str) -> str:
        """Validates that strings are not empty or solely whitespace."""
        if not v.strip():
            raise ValueError("Field must not be empty or whitespace-only")
        return v.strip()

    @field_validator("confidence_score")
    @classmethod
    def validate_confidence_score(cls, v: float | None) -> float | None:
        """Validates that the confidence score, if provided, is between 0.0 and 1.0."""
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("confidence_score must be between 0.0 and 1.0")
        return v


class AIMistake(BaseModel):
    """Represents an AI mistake record containing user correction and severity context."""

    mistake_id: str
    decision_id: str | None = None
    session_id: str
    project_id: str | None = None
    task_id: str | None = None
    timestamp: datetime
    provider: str
    model_id: str
    task_type: TaskType
    failure_type: FailureType
    failure_summary: str
    correction_made: bool = False
    correction_summary: str | None = None
    severity: Severity = Severity.medium
    auto_detected: bool = True
    detection_signal: str
    
    # Detection fields (optional, stored transitively or for testing and API responses)
    outcome_type: str | None = None
    user_corrected: bool | None = None
    correction_detail: str | None = None
    feedback_signal: float | None = None
    is_duplicate: bool = False

    @field_validator("provider", "model_id", "session_id", "failure_summary")
    @classmethod
    def validate_non_empty_strings(cls, v: str) -> str:
        """Validates that strings are not empty or solely whitespace."""
        if not v.strip():
            raise ValueError("Field must not be empty or whitespace-only")
        return v.strip()


class ProviderProfile(BaseModel):
    """Represents the compiled performance statistics of a provider-model-task type."""

    provider: str
    model_id: str
    provider_id: str = ""
    model_name: str = ""
    task_type: TaskType
    total_decisions: int = 0
    successful_decisions: int = 0
    mistake_count: int = 0
    user_reported_mistakes: int = 0
    correction_count: int = 0
    revert_count: int = 0
    heavy_edit_count: int = 0
    build_error_count: int = 0
    avg_feedback_score: float | None = None
    avg_latency_ms: float | None = None
    success_rate: float = 0.0
    acceptance_rate: float = 0.0
    modification_rate: float = 0.0
    rejection_rate: float = 0.0
    revert_rate: float = 0.0
    mistake_rate: float = 0.0
    correction_rate: float = 0.0
    heavy_edit_rate: float = 0.0
    build_error_rate: float = 0.0
    last_seen_at: datetime | None = None
    profile_confidence: str = "low"
    confidence_factor: float = 0.0
    quality_score: float = 0.0
    cold_start: bool = True
    sample_size_bucket: str = "tiny"
    mistake_by_type: dict[str, int] = Field(default_factory=dict)
    avg_confidence: float | None = None
    routing_score: float | None = None
    last_updated: datetime

    @model_validator(mode="after")
    def populate_provider_and_model_aliases(self) -> "ProviderProfile":
        if not self.provider_id:
            self.provider_id = self.provider
        if not self.model_name:
            self.model_name = self.model_id
        return self


class RoutingDecision(BaseModel):
    """Represents the context surrounding a provider routing decision event."""

    routing_id: str
    task_type: TaskType
    selected_provider: str
    selected_model: str
    recommended_provider: str = ""
    recommended_model: str = ""
    score: float | None = None
    quality_score: float | None = None
    profile_confidence: str | None = None
    confidence_factor: float | None = None
    competing_providers: list[dict[str, Any]] = Field(default_factory=list)
    ranked_candidates: list[dict[str, Any]] = Field(default_factory=list)
    cold_start: bool = False
    fallback_used: bool = False
    fallback_reason: str | None = None
    decision_source: str = "profile_ranked"
    timestamp: datetime

    @model_validator(mode="after")
    def populate_routing_aliases(self) -> "RoutingDecision":
        if not self.recommended_provider:
            self.recommended_provider = self.selected_provider
        if not self.recommended_model:
            self.recommended_model = self.selected_model
        if self.quality_score is None:
            self.quality_score = self.score
        if self.score is None:
            self.score = self.quality_score
        return self


# Request Payload Schemas
class CreateDecisionRequest(BaseModel):
    """Payload to request the creation of a new AI decision log."""

    session_id: str
    project_id: str | None = None
    task_id: str | None = None
    provider: str
    model_id: str
    task_type: TaskType
    suggestion_summary: str
    suggestion_hash: str
    user_action: UserAction
    modification_summary: str | None = None
    confidence_score: float | None = None
    routing_metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime | None = None

    @field_validator("provider", "model_id", "session_id", "suggestion_summary", "suggestion_hash")
    @classmethod
    def validate_non_empty_strings(cls, v: str) -> str:
        """Validates that strings are not empty or solely whitespace."""
        if not v.strip():
            raise ValueError("Field must not be empty or whitespace-only")
        return v.strip()

    @field_validator("confidence_score")
    @classmethod
    def validate_confidence_score(cls, v: float | None) -> float | None:
        """Validates that the confidence score, if provided, is between 0.0 and 1.0."""
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("confidence_score must be between 0.0 and 1.0")
        return v


class UpdateDecisionOutcomeRequest(BaseModel):
    """Payload to request the update of a logged decision's deferred outcome."""

    outcome: DecisionOutcome
    outcome_recorded_at: datetime | None = None


# Mistake Request Payload Schemas
class CreateMistakeRequest(BaseModel):
    """Payload to request the creation of a new AI mistake log."""

    decision_id: str | None = None
    session_id: str
    project_id: str | None = None
    task_id: str | None = None
    provider: str
    model_id: str
    task_type: TaskType
    failure_type: FailureType
    failure_summary: str
    correction_made: bool = False
    correction_summary: str | None = None
    severity: Severity = Severity.medium
    auto_detected: bool = False
    detection_signal: str = "EXPLICIT_REPORT"
    timestamp: datetime | None = None

    @field_validator("provider", "model_id", "session_id", "failure_summary")
    @classmethod
    def validate_non_empty_strings(cls, v: str) -> str:
        """Validates that strings are not empty or solely whitespace."""
        if not v.strip():
            raise ValueError("Field must not be empty or whitespace-only")
        return v.strip()


class FlagMistakeRequest(BaseModel):
    """Lighter payload for explicit user flagging of AI suggestions."""

    decision_id: str | None = None
    session_id: str
    project_id: str | None = None
    task_id: str | None = None
    provider: str
    model_id: str
    task_type: TaskType
    failure_summary: str
    failure_type: FailureType = FailureType.explicit_rejection
    severity: Severity = Severity.medium
    correction_summary: str | None = None
    timestamp: datetime | None = None

    @field_validator("provider", "model_id", "session_id", "failure_summary")
    @classmethod
    def validate_non_empty_strings(cls, v: str) -> str:
        """Validates that strings are not empty or solely whitespace."""
        if not v.strip():
            raise ValueError("Field must not be empty or whitespace-only")
        return v.strip()


class StatsSummaryResponse(BaseModel):
    total_providers: int
    total_models: int
    total_task_types: int
    total_decisions: int
    total_mistakes: int
    overall_success_rate: float
    overall_quality_score: float
    fallback_rate: float
    low_confidence_rate: float


class ProviderKPI(BaseModel):
    provider_id: str
    total_decisions: int
    success_rate: float
    mistake_rate: float
    correction_rate: float
    average_quality_score: float
    average_confidence_factor: float
    number_of_task_types_covered: int
    cold_start_provider_count: int


class ProviderDetailBreakdown(BaseModel):
    name: str
    total_decisions: int
    success_rate: float
    mistake_rate: float
    average_quality_score: float


class ProviderDetailResponse(BaseModel):
    provider_id: str
    total_decisions: int
    success_rate: float
    mistake_rate: float
    correction_rate: float
    average_quality_score: float
    average_confidence_factor: float
    number_of_task_types_covered: int
    cold_start_provider_count: int
    last_seen_timestamp: datetime | None = None
    decisions_last_7_days: int
    decisions_last_30_days: int
    task_type_breakdown: list[ProviderDetailBreakdown]
    model_breakdown: list[ProviderDetailBreakdown]


class TaskTypeKPI(BaseModel):
    task_type: str
    total_decisions: int
    successful_decisions: int
    mistakes: int
    average_quality_score: float
    number_of_profiles: int
    number_of_providers_with_high_confidence: int
    fallback_proportion: float
    coverage_quality: str


class LeaderboardEntry(BaseModel):
    rank: int
    provider_id: str
    model_name: str
    quality_score: float
    profile_confidence: str
    confidence_factor: float
    total_decisions: int
    success_rate: float
    mistake_rate: float
    cold_start: bool

