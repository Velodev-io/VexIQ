"""Unit tests for the VexIQ Pydantic data models.

Validates enum parsing, json serialization, default field initializations, 
and constraint validations for AIDecision, AIMistake, ProviderProfile, and 
RoutingDecision.
"""

from datetime import datetime, timezone
import json
from vexiq.models import (
    TaskType,
    UserAction,
    DecisionOutcome,
    FailureType,
    Severity,
    AIDecision,
    AIMistake,
    ProviderProfile,
    RoutingDecision,
)


def test_enums():
    """Verifies string values for enums map to expected outputs."""
    assert TaskType.code_edit == "code_edit"
    assert UserAction.accepted == "accepted"
    assert DecisionOutcome.unknown == "unknown"
    assert FailureType.wrong_code == "wrong_code"
    assert Severity.medium == "medium"


def test_ai_decision_serialization():
    """Verifies that AIDecision model serializes and parses properly."""
    now = datetime.now(timezone.utc)
    decision = AIDecision(
        decision_id="dec_123",
        session_id="sess_456",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="fixes bug",
        suggestion_hash="abc123hash",
        user_action=UserAction.accepted,
        confidence_score=0.95,
        routing_metadata={"metric": "latency", "val": 120},
    )

    js = decision.model_dump_json()
    data = json.loads(js)
    assert data["decision_id"] == "dec_123"
    assert data["user_action"] == "accepted"
    assert data["routing_metadata"] == {"metric": "latency", "val": 120}
    assert "timestamp" in data


def test_ai_mistake_serialization():
    """Verifies that AIMistake model serializes and parses properly."""
    now = datetime.now(timezone.utc)
    mistake = AIMistake(
        mistake_id="mist_123",
        decision_id="dec_123",
        session_id="sess_456",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        failure_type=FailureType.broken_build,
        failure_summary="syntax error",
        correction_made=True,
        correction_summary="added import",
        severity=Severity.high,
        auto_detected=True,
        detection_signal="BUILD_FAILURE",
    )

    js = mistake.model_dump_json()
    data = json.loads(js)
    assert data["mistake_id"] == "mist_123"
    assert data["failure_type"] == "broken_build"
    assert data["severity"] == "high"
    assert data["auto_detected"] is True


def test_provider_profile_defaults():
    """Verifies default values are correctly instantiated in ProviderProfile."""
    profile = ProviderProfile(
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        last_updated=datetime.now(timezone.utc),
    )
    assert profile.total_decisions == 0
    assert profile.acceptance_rate == 0.0
    assert profile.revert_rate == 0.0
    assert profile.mistake_by_type == {}
    assert profile.routing_score is None


def test_routing_decision_defaults():
    """Verifies default values are correctly instantiated in RoutingDecision."""
    routing = RoutingDecision(
        routing_id="route_123",
        task_type=TaskType.code_edit,
        selected_provider="openai",
        selected_model="gpt-4o",
        timestamp=datetime.now(timezone.utc),
    )
    assert routing.score is None
    assert routing.competing_providers == []
    assert routing.cold_start is False
