"""Tests for the VexIQ Decision Logger core module.

Verifies correct recording of suggestions, validation of user action updates, 
and deferred outcomes matching logic.
"""

from datetime import datetime, timezone, timedelta
import pytest
import pytest_asyncio

from vexiq.models import AIDecision, TaskType, UserAction, DecisionOutcome
from vexiq.db import init_db
from vexiq.core.decision_logger import DecisionLogger


@pytest_asyncio.fixture
async def setup_test_db(tmp_path):
    """Initializes an isolated SQLite database file for testing."""
    db_file = tmp_path / "vexiq_test_logger.db"
    db_path = str(db_file)
    await init_db(db_path)
    return db_path


@pytest.mark.asyncio
async def test_create_and_get_decision(setup_test_db):
    """Tests the standard creation and retrieval of AIDecision records."""
    db_path = setup_test_db
    logger = DecisionLogger(db_path)

    decision = AIDecision(
        decision_id="dec_1",
        session_id="sess_1",
        timestamp=datetime.now(timezone.utc),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_1",
        user_action=UserAction.accepted,
        confidence_score=0.9,
    )

    # Test creation
    created = await logger.create_decision(decision)
    assert created.decision_id == "dec_1"

    # Test retrieval
    retrieved = await logger.get_decision("dec_1")
    assert retrieved is not None
    assert retrieved.decision_id == "dec_1"
    assert retrieved.session_id == "sess_1"
    assert retrieved.provider == "openai"
    assert retrieved.outcome == DecisionOutcome.unknown


@pytest.mark.asyncio
async def test_update_outcome(setup_test_db):
    """Tests patching outcome fields on existing and missing decisions."""
    db_path = setup_test_db
    logger = DecisionLogger(db_path)

    decision = AIDecision(
        decision_id="dec_1",
        session_id="sess_1",
        timestamp=datetime.now(timezone.utc),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_1",
        user_action=UserAction.accepted,
    )
    await logger.create_decision(decision)

    # Test outcome update
    updated = await logger.update_outcome("dec_1", DecisionOutcome.kept)
    assert updated is not None
    assert updated.outcome == DecisionOutcome.kept
    assert updated.outcome_recorded_at is not None

    # Retrieve to check persistence
    retrieved = await logger.get_decision("dec_1")
    assert retrieved.outcome == DecisionOutcome.kept

    # Test update missing decision returns None
    missing = await logger.update_outcome("dec_missing", DecisionOutcome.kept)
    assert missing is None


@pytest.mark.asyncio
async def test_list_recent_decisions(setup_test_db):
    """Tests listing logged decisions, asserting sorting and limits."""
    db_path = setup_test_db
    logger = DecisionLogger(db_path)

    now = datetime.now(timezone.utc)
    for i in range(5):
        d = AIDecision(
            decision_id=f"dec_{i}",
            session_id="sess_1",
            timestamp=now + timedelta(seconds=i),
            provider="openai",
            model_id="gpt-4o",
            task_type=TaskType.code_edit,
            suggestion_summary="summary",
            suggestion_hash=f"hash_{i}",
            user_action=UserAction.accepted,
        )
        await logger.create_decision(d)

    recent = await logger.list_recent_decisions(limit=3)
    assert len(recent) == 3
    # Order should be timestamp descending
    assert recent[0].decision_id == "dec_4"
    assert recent[1].decision_id == "dec_3"
    assert recent[2].decision_id == "dec_2"


@pytest.mark.asyncio
async def test_duplicate_safeguard(setup_test_db):
    """Tests that duplicate event emissions within a 10s window resolve to the original record."""
    db_path = setup_test_db
    logger = DecisionLogger(db_path)

    now = datetime.now(timezone.utc)
    d1 = AIDecision(
        decision_id="dec_first",
        session_id="sess_1",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_dup",
        user_action=UserAction.accepted,
    )

    created1 = await logger.create_decision(d1)
    assert created1.decision_id == "dec_first"

    # Create matching decision shortly after (within 10s window)
    d2 = AIDecision(
        decision_id="dec_second",
        session_id="sess_1",
        timestamp=now + timedelta(seconds=5),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_dup",
        user_action=UserAction.accepted,
    )

    created2 = await logger.create_decision(d2)
    # Safeguard should return the first decision
    assert created2.decision_id == "dec_first"

    # Outside the window (25 seconds later)
    d3 = AIDecision(
        decision_id="dec_third",
        session_id="sess_1",
        timestamp=now + timedelta(seconds=25),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_dup",
        user_action=UserAction.accepted,
    )
    created3 = await logger.create_decision(d3)
    # Should create a new decision
    assert created3.decision_id == "dec_third"
