"""Tests for the VexIQ Routing Engine.

Verifies candidate ranking, fallback states, tie-breaking rules,
and explainability logic.
"""

from datetime import datetime, timezone
import pytest
import pytest_asyncio

from vexiq.models import (
    AIDecision,
    TaskType,
    UserAction,
    DecisionOutcome,
    ProviderProfile,
)
from vexiq.db import init_db, insert_decision, upsert_provider_profile
from vexiq.core.provider_profile import ProviderProfileBuilder
from vexiq.core.routing_engine import RoutingEngine, DEFAULT_ROUTING_FALLBACKS


@pytest_asyncio.fixture
async def setup_test_db(tmp_path):
    """Initializes an isolated SQLite database file for testing."""
    db_file = tmp_path / "vexiq_test_routing.db"
    db_path = str(db_file)
    await init_db(db_path)
    return db_path


@pytest.mark.asyncio
async def test_warm_path_recommendation(setup_test_db):
    """Verifies that the warm path recommendation selects the highest-scoring candidate."""
    db_path = setup_test_db
    engine = RoutingEngine(db_path)
    now = datetime.now(timezone.utc)

    # Seed 50 decisions for openai/gpt-4o/code (perfect success, quality_score ~ 0.45)
    for i in range(50):
        d1 = AIDecision(
            decision_id=f"dec_a_{i}",
            session_id="sess_123",
            timestamp=now,
            provider="openai",
            model_id="gpt-4o",
            task_type=TaskType.code,
            suggestion_summary="summary",
            suggestion_hash=f"hash_a_{i}",
            user_action=UserAction.accepted,
            outcome=DecisionOutcome.kept,
        )
        await insert_decision(db_path, d1)

    # Seed 50 decisions for anthropic/claude/code (moderate success, quality_score ~ 0.35)
    for i in range(50):
        d2 = AIDecision(
            decision_id=f"dec_b_{i}",
            session_id="sess_123",
            timestamp=now,
            provider="anthropic",
            model_id="claude",
            task_type=TaskType.code,
            suggestion_summary="summary",
            suggestion_hash=f"hash_b_{i}",
            user_action=UserAction.accepted,
            outcome=DecisionOutcome.kept if i < 30 else DecisionOutcome.reverted,
        )
        await insert_decision(db_path, d2)

    # Refresh profiles builder cache
    profile_builder = ProviderProfileBuilder(db_path)
    await profile_builder.refresh_provider_profiles()

    decision = await engine.recommend_provider("code")
    assert decision.recommended_provider == "openai"
    assert decision.recommended_model == "gpt-4o"
    assert decision.fallback_used is False
    assert decision.decision_source == "profile_ranked"
    assert len(decision.ranked_candidates) == 2
    assert decision.ranked_candidates[0]["provider_id"] == "openai"


@pytest.mark.asyncio
async def test_routing_tie_breaking(setup_test_db):
    """Verifies tie-breaking order: higher confidence, non-cold-start, total decisions."""
    db_path = setup_test_db
    engine = RoutingEngine(db_path)
    now = datetime.now(timezone.utc)

    # We will upsert custom profiles directly with identical quality score (0.60)
    # Profile A: low confidence
    p_a = ProviderProfile(
        provider="prov_a",
        model_id="mod_a",
        task_type=TaskType.code,
        total_decisions=10,
        quality_score=0.60,
        profile_confidence="low",
        confidence_factor=0.2,
        cold_start=True,
        last_updated=now,
    )
    # Profile B: medium confidence
    p_b = ProviderProfile(
        provider="prov_b",
        model_id="mod_b",
        task_type=TaskType.code,
        total_decisions=25,
        quality_score=0.60,
        profile_confidence="medium",
        confidence_factor=0.5,
        cold_start=False,
        last_updated=now,
    )
    # Profile C: high confidence
    p_c = ProviderProfile(
        provider="prov_c",
        model_id="mod_c",
        task_type=TaskType.code,
        total_decisions=50,
        quality_score=0.60,
        profile_confidence="high",
        confidence_factor=1.0,
        cold_start=False,
        last_updated=now,
    )

    await upsert_provider_profile(db_path, p_a)
    await upsert_provider_profile(db_path, p_b)
    await upsert_provider_profile(db_path, p_c)

    decision = await engine.recommend_provider("code")
    # Should select prov_c because of higher confidence
    assert decision.recommended_provider == "prov_c"
    assert decision.ranked_candidates[0]["provider_id"] == "prov_c"
    assert decision.ranked_candidates[1]["provider_id"] == "prov_b"
    assert decision.ranked_candidates[2]["provider_id"] == "prov_a"


@pytest.mark.asyncio
async def test_fallback_case_1_no_history(setup_test_db):
    """Verifies Fallback Case 1: no database profiles exist, returning static fallback defaults."""
    db_path = setup_test_db
    engine = RoutingEngine(db_path)

    # Empty database
    decision = await engine.recommend_provider("chat")
    assert decision.fallback_used is True
    assert decision.decision_source == "cold_start_default"
    assert decision.fallback_reason == "no_profile_data"

    # Verify static defaults matches first element in Fallbacks config
    expected_prov, expected_mod = DEFAULT_ROUTING_FALLBACKS["chat"][0]
    assert decision.recommended_provider == expected_prov
    assert decision.recommended_model == expected_mod
    assert len(decision.ranked_candidates) == len(DEFAULT_ROUTING_FALLBACKS["chat"])


@pytest.mark.asyncio
async def test_fallback_case_2_low_confidence(setup_test_db):
    """Verifies Fallback Case 2: profile exists but sample size is too small (low confidence)."""
    db_path = setup_test_db
    engine = RoutingEngine(db_path)
    now = datetime.now(timezone.utc)

    # Seed 1 decision for openai/gpt-4o/code (100% success, but total_decisions=1 < 10)
    d = AIDecision(
        decision_id="dec_low_c",
        session_id="sess_123",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        suggestion_summary="summary",
        suggestion_hash="hash_low_c",
        user_action=UserAction.accepted,
        outcome=DecisionOutcome.kept,
    )
    await insert_decision(db_path, d)

    profile_builder = ProviderProfileBuilder(db_path)
    await profile_builder.refresh_provider_profiles()

    decision = await engine.recommend_provider("code")
    # Should use the candidate but flag it as low confidence fallback
    assert decision.recommended_provider == "openai"
    assert decision.fallback_used is True
    assert decision.decision_source == "low_confidence_fallback"
    assert decision.fallback_reason == "insufficient_history"


@pytest.mark.asyncio
async def test_fallback_case_3_poor_quality(setup_test_db):
    """Verifies Fallback Case 3: candidates exist but all scores are below the minimum threshold."""
    db_path = setup_test_db
    engine = RoutingEngine(db_path)
    now = datetime.now(timezone.utc)

    # Directly upsert a poor quality candidate with high confidence (total_decisions=50)
    p = ProviderProfile(
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        total_decisions=50,
        quality_score=0.20,  # Below threshold 0.35
        profile_confidence="high",
        confidence_factor=1.0,
        cold_start=False,
        last_updated=now,
    )
    await upsert_provider_profile(db_path, p)

    decision = await engine.recommend_provider("code")
    # All candidates are below 0.35, so it triggers Fallback Case 3 static default
    assert decision.fallback_used is True
    assert decision.decision_source == "low_quality_fallback"
    assert decision.fallback_reason == "all_candidates_below_threshold"

    # Static default priority 1 for code is claude-3.5-sonnet
    expected_prov, expected_mod = DEFAULT_ROUTING_FALLBACKS["code"][0]
    assert decision.recommended_provider == expected_prov
    assert decision.recommended_model == expected_mod


@pytest.mark.asyncio
async def test_explain_recommendation(setup_test_db):
    """Verifies explanation metadata contains the decision reasoning trace."""
    db_path = setup_test_db
    engine = RoutingEngine(db_path)

    explanation = await engine.explain_recommendation("chat")
    assert explanation["task_type"] == "chat"
    assert "recommended_provider" in explanation
    assert "explanation" in explanation
    assert explanation["fallback_used"] is True
    assert "cold_start_default" in explanation["explanation"]


@pytest.mark.asyncio
async def test_invalid_task_type_raises(setup_test_db):
    """Verifies that an invalid task type string raises ValueError."""
    db_path = setup_test_db
    engine = RoutingEngine(db_path)

    with pytest.raises(ValueError) as exc_info:
        await engine.recommend_provider("invalid_type")
    assert "Invalid task_type" in str(exc_info.value)
