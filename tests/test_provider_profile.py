"""Tests for the VexIQ Provider Profile builder.

Verifies calculation of aggregated metrics (acceptance rate, mistake rate,
revert rate) based on database decision logs.
"""

from datetime import datetime, timezone, timedelta
import pytest
import pytest_asyncio

from vexiq.models import (
    AIDecision,
    AIMistake,
    TaskType,
    UserAction,
    DecisionOutcome,
    FailureType,
    Severity,
    ProviderProfile,
)
from vexiq.db import init_db, insert_decision, insert_mistake, get_provider_profile_by_keys, list_provider_profiles_from_db
from vexiq.core.provider_profile import ProviderProfileBuilder


@pytest_asyncio.fixture
async def setup_test_db(tmp_path):
    """Initializes an isolated SQLite database file for testing."""
    db_file = tmp_path / "vexiq_test_profiles.db"
    db_path = str(db_file)
    await init_db(db_path)
    return db_path


@pytest.mark.asyncio
async def test_cold_start_profile(setup_test_db):
    """Verifies that querying a combination with no history returns a default cold-start profile."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)

    profile = await builder.build_provider_profile("openai", "gpt-4o", "code_edit")
    assert profile.provider == "openai"
    assert profile.model_id == "gpt-4o"
    assert profile.provider_id == "openai"
    assert profile.model_name == "gpt-4o"
    assert profile.task_type == TaskType.code_edit
    assert profile.total_decisions == 0
    assert profile.cold_start is True
    assert profile.quality_score == 0.30
    assert profile.profile_confidence == "low"
    assert profile.sample_size_bucket == "tiny"
    assert profile.mistake_by_type == {}


@pytest.mark.asyncio
async def test_happy_path_aggregation(setup_test_db):
    """Verifies profile computations (counts, rates, averages, quality scores) on seeded history."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)
    now = datetime.now(timezone.utc)

    # Seed 10 decisions for openai/gpt-4o/code_edit
    # 8 kept (successful), 2 reverted (revert_count=2, which is 1 explicit + 1 signal)
    # Latency: alternating 200ms and 300ms -> avg 250ms
    # Feedback: alternating 4.0 and 5.0 -> avg 4.5
    for i in range(10):
        outcome = DecisionOutcome.kept if i < 8 else DecisionOutcome.reverted
        user_action = UserAction.accepted if i < 7 else (UserAction.modified if i < 9 else UserAction.rejected)
        
        d = AIDecision(
            decision_id=f"dec_{i}",
            session_id="sess_123",
            timestamp=now - timedelta(minutes=10 - i),
            provider="openai",
            model_id="gpt-4o",
            task_type=TaskType.code_edit,
            suggestion_summary="summary",
            suggestion_hash=f"hash_{i}",
            user_action=user_action,
            outcome=outcome,
            confidence_score=0.9,
            routing_metadata={"latency_ms": 200 if i % 2 == 0 else 300, "feedback_score": 4.0 if i % 2 == 0 else 5.0},
        )
        await insert_decision(db_path, d)

    # Seed 2 mistakes linked to decisions
    m1 = AIMistake(
        mistake_id="mist_1",
        decision_id="dec_8",
        session_id="sess_123",
        timestamp=now - timedelta(minutes=2),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_code,
        failure_summary="broken syntax",
        correction_made=True,
        correction_summary="fixed syntax",
        severity=Severity.medium,
        auto_detected=True,
        detection_signal="file_revert",
    )
    await insert_mistake(db_path, m1)

    m2 = AIMistake(
        mistake_id="mist_2",
        decision_id="dec_9",
        session_id="sess_123",
        timestamp=now - timedelta(minutes=1),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        failure_type=FailureType.broken_build,
        failure_summary="broken build",
        correction_made=False,
        severity=Severity.high,
        auto_detected=True,
        detection_signal="build_failure",
    )
    await insert_mistake(db_path, m2)

    profile = await builder.build_provider_profile("openai", "gpt-4o", "code_edit")
    assert profile.total_decisions == 10
    assert profile.successful_decisions == 8
    assert profile.mistake_count == 2
    assert profile.success_rate == 0.8
    assert profile.mistake_rate == 0.2
    assert profile.revert_rate == 0.1  # 1 file_revert signal
    assert profile.build_error_rate == 0.1  # 1 build_failure signal
    assert profile.correction_rate == 0.1  # 1 correction_made
    assert profile.avg_latency_ms == 250.0
    assert profile.avg_feedback_score == 4.5
    assert profile.profile_confidence == "medium"
    assert profile.sample_size_bucket == "medium"
    assert profile.cold_start is False
    assert profile.mistake_by_type == {"wrong_code": 1, "broken_build": 1}

    # Verify quality score math:
    # normalized_feedback = 4.5 / 5.0 = 0.9
    # latency_penalty = 250.0 / 5000.0 = 0.05
    # raw_score = 0.35*0.8 + 0.20*0.9 - 0.20*0.2 - 0.10*0.1 - 0.10*0.1 - 0.05*0.05
    #           = 0.28 + 0.18 - 0.04 - 0.01 - 0.01 - 0.0025 = 0.3975
    # confidence_factor = 10 / 50 = 0.2
    # blended_score = 0.3975 * 0.2 + 0.30 * 0.8 = 0.0795 + 0.24 = 0.3195
    assert pytest.approx(profile.quality_score, abs=1e-4) == 0.3195


@pytest.mark.asyncio
async def test_strong_vs_weak_ranking(setup_test_db):
    """Verifies that a provider with superior performance is ranked above a weaker provider."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)
    now = datetime.now(timezone.utc)

    # Provider A: 50 decisions, 48 successes, 0 mistakes (Strong)
    for i in range(50):
        d = AIDecision(
            decision_id=f"dec_a_{i}",
            session_id="sess_123",
            timestamp=now,
            provider="openai",
            model_id="gpt-4o",
            task_type=TaskType.code_edit,
            suggestion_summary="summary",
            suggestion_hash=f"hash_a_{i}",
            user_action=UserAction.accepted,
            outcome=DecisionOutcome.kept,
        )
        await insert_decision(db_path, d)

    # Provider B: 50 decisions, 30 successes, 20 mistakes (Weak)
    for i in range(50):
        d = AIDecision(
            decision_id=f"dec_b_{i}",
            session_id="sess_123",
            timestamp=now,
            provider="anthropic",
            model_id="claude",
            task_type=TaskType.code_edit,
            suggestion_summary="summary",
            suggestion_hash=f"hash_b_{i}",
            user_action=UserAction.accepted,
            outcome=DecisionOutcome.kept if i < 30 else DecisionOutcome.reverted,
        )
        await insert_decision(db_path, d)

    for i in range(20):
        m = AIMistake(
            mistake_id=f"mist_b_{i}",
            decision_id=f"dec_b_{30+i}",
            session_id="sess_123",
            timestamp=now,
            provider="anthropic",
            model_id="claude",
            task_type=TaskType.code_edit,
            failure_type=FailureType.wrong_code,
            failure_summary="bug",
            detection_signal="file_revert",
        )
        await insert_mistake(db_path, m)

    top_profiles = await builder.get_top_profiles("code_edit", limit=5)
    assert len(top_profiles) == 2
    assert top_profiles[0].provider == "openai"
    assert top_profiles[1].provider == "anthropic"
    assert top_profiles[0].quality_score > top_profiles[1].quality_score


@pytest.mark.asyncio
async def test_low_sample_confidence_blending(setup_test_db):
    """Verifies that a tiny dataset with a perfect success rate does not outrank a proven provider."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)
    now = datetime.now(timezone.utc)

    # Provider A (Tiny): 1 decision, 1 success (100% success rate, 0 mistakes)
    d_a = AIDecision(
        decision_id="dec_a_1",
        session_id="sess_123",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_a_1",
        user_action=UserAction.accepted,
        outcome=DecisionOutcome.kept,
    )
    await insert_decision(db_path, d_a)

    # Provider B (Strong): 50 decisions, 45 successes, 5 reverts
    for i in range(50):
        d_b = AIDecision(
            decision_id=f"dec_b_{i}",
            session_id="sess_123",
            timestamp=now,
            provider="anthropic",
            model_id="claude",
            task_type=TaskType.code_edit,
            suggestion_summary="summary",
            suggestion_hash=f"hash_b_{i}",
            user_action=UserAction.accepted,
            outcome=DecisionOutcome.kept if i < 45 else DecisionOutcome.reverted,
        )
        await insert_decision(db_path, d_b)

    top_profiles = await builder.get_top_profiles("code_edit", limit=5)
    assert len(top_profiles) == 2
    # Strong provider B should rank first because Tiny provider A blends heavily towards the 0.50 baseline
    assert top_profiles[0].provider == "anthropic"
    assert top_profiles[1].provider == "openai"


@pytest.mark.asyncio
async def test_task_type_isolation(setup_test_db):
    """Verifies that decisions under one task category do not affect metrics of another category."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)
    now = datetime.now(timezone.utc)

    # Seed 10 decisions for code_edit (100% success)
    for i in range(10):
        d = AIDecision(
            decision_id=f"dec_code_{i}",
            session_id="sess_123",
            timestamp=now,
            provider="openai",
            model_id="gpt-4o",
            task_type=TaskType.code_edit,
            suggestion_summary="summary",
            suggestion_hash=f"hash_code_{i}",
            user_action=UserAction.accepted,
            outcome=DecisionOutcome.kept,
        )
        await insert_decision(db_path, d)

    # Seed 5 decisions for command (0% success / reverted)
    for i in range(5):
        d = AIDecision(
            decision_id=f"dec_cmd_{i}",
            session_id="sess_123",
            timestamp=now,
            provider="openai",
            model_id="gpt-4o",
            task_type=TaskType.command,
            suggestion_summary="summary",
            suggestion_hash=f"hash_cmd_{i}",
            user_action=UserAction.accepted,
            outcome=DecisionOutcome.reverted,
        )
        await insert_decision(db_path, d)

    code_profile = await builder.build_provider_profile("openai", "gpt-4o", "code_edit")
    assert code_profile.total_decisions == 10
    assert code_profile.success_rate == 1.0

    cmd_profile = await builder.build_provider_profile("openai", "gpt-4o", "command")
    assert cmd_profile.total_decisions == 5
    assert cmd_profile.success_rate == 0.0


@pytest.mark.asyncio
async def test_null_safe_handling(setup_test_db):
    """Verifies that missing feedback or latency in routing metadata does not cause aggregation to crash."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)
    now = datetime.now(timezone.utc)

    d = AIDecision(
        decision_id="dec_null",
        session_id="sess_123",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_null",
        user_action=UserAction.accepted,
        outcome=DecisionOutcome.kept,
        # No feedback or latency in metadata
        routing_metadata={},
    )
    await insert_decision(db_path, d)

    profile = await builder.build_provider_profile("openai", "gpt-4o", "code_edit")
    assert profile.total_decisions == 1
    assert profile.avg_feedback_score is None
    assert profile.avg_latency_ms is None
    # Calculations should succeed and clamp gracefully
    assert profile.quality_score is not None


@pytest.mark.asyncio
async def test_last_seen_timestamp(setup_test_db):
    """Verifies that last_seen_at correctly captures the latest timestamp from decisions or mistakes."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)
    base_time = datetime.now(timezone.utc)

    # Seed decision at base_time
    d = AIDecision(
        decision_id="dec_t",
        session_id="sess_123",
        timestamp=base_time,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_t",
        user_action=UserAction.accepted,
        outcome=DecisionOutcome.kept,
    )
    await insert_decision(db_path, d)

    # Seed mistake at base_time + 1 hour (more recent)
    m = AIMistake(
        mistake_id="mist_t",
        decision_id="dec_t",
        session_id="sess_123",
        timestamp=base_time + timedelta(hours=1),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_code,
        failure_summary="bug",
        detection_signal="file_revert",
    )
    await insert_mistake(db_path, m)

    profile = await builder.build_provider_profile("openai", "gpt-4o", "code_edit")
    # Verify last_seen_at matches the more recent mistake timestamp
    assert profile.last_seen_at == base_time + timedelta(hours=1)


@pytest.mark.asyncio
async def test_duplicate_inflation_prevention(setup_test_db):
    """Verifies that multiple mistakes linked to a single decision do not inflate the total decisions count."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)
    now = datetime.now(timezone.utc)

    # 1 Decision
    d = AIDecision(
        decision_id="dec_dup",
        session_id="sess_123",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_dup",
        user_action=UserAction.accepted,
        outcome=DecisionOutcome.reverted,
    )
    await insert_decision(db_path, d)

    # 2 Mistakes linked to the same decision_id
    for i in range(2):
        m = AIMistake(
            mistake_id=f"mist_dup_{i}",
            decision_id="dec_dup",
            session_id="sess_123",
            timestamp=now,
            provider="openai",
            model_id="gpt-4o",
            task_type=TaskType.code_edit,
            failure_type=FailureType.wrong_code,
            failure_summary=f"bug {i}",
            detection_signal="file_revert",
        )
        await insert_mistake(db_path, m)

    profile = await builder.build_provider_profile("openai", "gpt-4o", "code_edit")
    # Decision count must remain 1 (not inflated to 2 via JOIN multiplication)
    assert profile.total_decisions == 1
    # Mistake count is correctly aggregated as 2
    assert profile.mistake_count == 2
    assert profile.mistake_rate == 2.0


@pytest.mark.asyncio
async def test_refresh_provider_profiles_caching(setup_test_db):
    """Verifies refresh_provider_profiles saves aggregated results to the DB cache and can retrieve them."""
    db_path = setup_test_db
    builder = ProviderProfileBuilder(db_path)
    now = datetime.now(timezone.utc)

    d = AIDecision(
        decision_id="dec_c",
        session_id="sess_123",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_c",
        user_action=UserAction.accepted,
        outcome=DecisionOutcome.kept,
    )
    await insert_decision(db_path, d)

    # Run refresh
    count = await builder.refresh_provider_profiles()
    assert count == 1

    # Load from DB directly using helper functions
    cached = await get_provider_profile_by_keys(db_path, "openai", "gpt-4o", "code_edit")
    assert cached is not None
    assert cached.provider == "openai"
    assert cached.model_id == "gpt-4o"
    assert cached.total_decisions == 1
    assert cached.cold_start is True  # 1 decision is < 5, so still cold start

    # List all profiles from DB
    all_cached = await list_provider_profiles_from_db(db_path)
    assert len(all_cached) == 1
    assert all_cached[0].provider == "openai"
