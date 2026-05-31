"""Tests for the VexIQ Mistake Tracker core module.

Verifies mistake persistence, duplicate protection safeguards, explicit user flagging,
and decision link reference validation.
"""

from datetime import datetime, timezone, timedelta
import pytest
import pytest_asyncio

from vexiq.models import (
    AIDecision,
    AIMistake,
    FlagMistakeRequest,
    TaskType,
    UserAction,
    FailureType,
    Severity,
)
from vexiq.db import init_db, insert_decision
from vexiq.core.mistake_tracker import MistakeTracker


@pytest_asyncio.fixture
async def setup_test_db(tmp_path):
    """Initializes an isolated SQLite database file for testing."""
    db_file = tmp_path / "vexiq_test_mistakes.db"
    db_path = str(db_file)
    await init_db(db_path)
    return db_path


@pytest.mark.asyncio
async def test_create_and_get_mistake(setup_test_db):
    """Tests basic creation and retrieval of AIMistake records."""
    db_path = setup_test_db
    tracker = MistakeTracker(db_path)

    mistake = AIMistake(
        mistake_id="mistake_1",
        session_id="sess_1",
        timestamp=datetime.now(timezone.utc),
        provider="anthropic",
        model_id="claude-3-5-sonnet",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_code,
        failure_summary="Generated broken syntax",
        correction_made=True,
        correction_summary="Fixed typos",
        severity=Severity.medium,
        auto_detected=False,
        detection_signal="EXPLICIT_REPORT",
    )

    created = await tracker.create_mistake(mistake)
    assert created.mistake_id == "mistake_1"

    retrieved = await tracker.get_mistake("mistake_1")
    assert retrieved is not None
    assert retrieved.mistake_id == "mistake_1"
    assert retrieved.session_id == "sess_1"
    assert retrieved.failure_type == FailureType.wrong_code
    assert retrieved.correction_made is True


@pytest.mark.asyncio
async def test_list_recent_mistakes(setup_test_db):
    """Tests retrieval of multiple mistakes ordered by timestamp descending."""
    db_path = setup_test_db
    tracker = MistakeTracker(db_path)
    now = datetime.now(timezone.utc)

    m1 = AIMistake(
        mistake_id="m1",
        session_id="sess_1",
        timestamp=now - timedelta(minutes=5),
        provider="anthropic",
        model_id="claude-3",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_code,
        failure_summary="f1",
        detection_signal="EXPLICIT",
    )
    m2 = AIMistake(
        mistake_id="m2",
        session_id="sess_1",
        timestamp=now,
        provider="anthropic",
        model_id="claude-3",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_command,
        failure_summary="f2",
        detection_signal="EXPLICIT",
    )

    await tracker.create_mistake(m1)
    await tracker.create_mistake(m2)

    recent = await tracker.list_recent_mistakes(limit=10)
    assert len(recent) == 2
    assert recent[0].mistake_id == "m2"  # More recent timestamp first
    assert recent[1].mistake_id == "m1"


@pytest.mark.asyncio
async def test_decision_existence_validation(setup_test_db):
    """Verifies that creating a mistake with a missing decision_id fails, and succeeds if it exists."""
    db_path = setup_test_db
    tracker = MistakeTracker(db_path)

    mistake = AIMistake(
        mistake_id="mistake_with_decision",
        decision_id="nonexistent_decision_id",
        session_id="sess_1",
        timestamp=datetime.now(timezone.utc),
        provider="openai",
        model_id="gpt-4",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_code,
        failure_summary="Missing reference",
        detection_signal="EXPLICIT",
    )

    # Should raise ValueError because decision doesn't exist
    with pytest.raises(ValueError) as exc_info:
        await tracker.create_mistake(mistake)
    assert "does not exist" in str(exc_info.value)

    # Now register/mock the decision in the database
    decision = AIDecision(
        decision_id="existing_decision_id",
        session_id="sess_1",
        timestamp=datetime.now(timezone.utc),
        provider="openai",
        model_id="gpt-4",
        task_type=TaskType.code_edit,
        suggestion_summary="Suggestion",
        suggestion_hash="hash_abc",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, decision)

    # Update mistake with valid decision_id and try again
    mistake.decision_id = "existing_decision_id"
    created = await tracker.create_mistake(mistake)
    assert created.mistake_id == "mistake_with_decision"


@pytest.mark.asyncio
async def test_duplicate_safeguard(setup_test_db):
    """Verifies that mistakes logged within the 30-second window return the duplicate."""
    db_path = setup_test_db
    tracker = MistakeTracker(db_path)
    now = datetime.now(timezone.utc)

    mistake1 = AIMistake(
        mistake_id="first_m",
        session_id="sess_1",
        timestamp=now,
        provider="google",
        model_id="gemini",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_config,
        failure_summary="Bad yaml syntax",
        detection_signal="EXPLICIT",
    )

    # Within the 30-second window (10 seconds later)
    mistake2 = AIMistake(
        mistake_id="second_m",
        session_id="sess_1",
        timestamp=now + timedelta(seconds=10),
        provider="google",
        model_id="gemini",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_config,
        failure_summary="Bad yaml syntax",
        detection_signal="EXPLICIT",
    )

    # Outside the window (45 seconds later)
    mistake3 = AIMistake(
        mistake_id="third_m",
        session_id="sess_1",
        timestamp=now + timedelta(seconds=45),
        provider="google",
        model_id="gemini",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_config,
        failure_summary="Bad yaml syntax",
        detection_signal="EXPLICIT",
    )

    created1 = await tracker.create_mistake(mistake1)
    assert created1.mistake_id == "first_m"

    created2 = await tracker.create_mistake(mistake2)
    assert created2.mistake_id == "first_m"  # returns duplicate

    created3 = await tracker.create_mistake(mistake3)
    assert created3.mistake_id == "third_m"  # outside window, new insert


@pytest.mark.asyncio
async def test_flag_mistake_mapping(setup_test_db):
    """Tests that flag_mistake converts FlagMistakeRequest properties correctly."""
    db_path = setup_test_db
    tracker = MistakeTracker(db_path)
    now = datetime.now(timezone.utc)

    request = FlagMistakeRequest(
        session_id="sess_abc",
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        failure_summary="Hallucinated API method",
        severity=Severity.high,
        correction_summary="Substituted correct module import",
        timestamp=now,
    )

    mistake = await tracker.flag_mistake(request)
    assert mistake.session_id == "sess_abc"
    assert mistake.provider == "openai"
    assert mistake.model_id == "gpt-4o"
    assert mistake.task_type == TaskType.code_edit
    assert mistake.failure_type == FailureType.explicit_rejection  # Default
    assert mistake.failure_summary == "Hallucinated API method"
    assert mistake.severity == Severity.high
    assert mistake.correction_summary == "Substituted correct module import"
    assert mistake.correction_made is True  # Inferred from summary presence
    assert mistake.auto_detected is False  # Set False for explicit flag
    assert mistake.detection_signal == "EXPLICIT_FLAG"
