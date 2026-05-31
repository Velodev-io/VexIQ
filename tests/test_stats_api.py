"""Integration tests for the VexIQ Stats API endpoints.

Verifies summary KPIs, provider performance statistics, task type coverage,
leaderboard rankings, empty-db safety, and filters.
"""

from datetime import datetime, timezone, timedelta
import os
import pytest
from fastapi.testclient import TestClient

from vexiq.config import get_settings
from vexiq.main import app
from vexiq.models import AIDecision, AIMistake, TaskType, UserAction, DecisionOutcome, FailureType, Severity, ProviderProfile
from vexiq.db import init_db, insert_decision, insert_mistake, upsert_provider_profile
from vexiq.core.provider_profile import ProviderProfileBuilder


@pytest.fixture(scope="module", autouse=True)
def setup_test_env(tmp_path_factory):
    """Generates settings overrides using a temporary database path and environment injection."""
    tmp_db_dir = tmp_path_factory.mktemp("vexiq_test_stats_api")
    db_path = str(tmp_db_dir / "vexiq.db")

    old_db_path = os.environ.get("VEXIQ_DB_PATH")
    old_port = os.environ.get("VEXIQ_PORT")

    os.environ["VEXIQ_DB_PATH"] = db_path
    os.environ["VEXIQ_PORT"] = "9999"
    get_settings.cache_clear()

    yield db_path

    if old_db_path is not None:
        os.environ["VEXIQ_DB_PATH"] = old_db_path
    else:
        os.environ.pop("VEXIQ_DB_PATH", None)

    if old_port is not None:
        os.environ["VEXIQ_PORT"] = old_port
    else:
        os.environ.pop("VEXIQ_PORT", None)

    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client():
    """Sets up the TestClient for isolated testing."""
    with TestClient(app) as c:
        yield c


def test_stats_empty_db(client):
    """Verifies analytics endpoints return safe default values when database is empty."""
    # Summary
    response = client.get("/stats/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["total_providers"] == 0
    assert data["total_decisions"] == 0
    assert data["total_mistakes"] == 0
    assert data["overall_success_rate"] == 0.0

    # Providers
    response = client.get("/stats/providers")
    assert response.status_code == 200
    assert response.json() == []

    # Task Types
    response = client.get("/stats/task-types")
    assert response.status_code == 200
    assert response.json() == []

    # Leaderboard
    response = client.get("/stats/leaderboard")
    assert response.status_code == 200
    assert response.json() == []


def test_stats_aggregation_success(client, setup_test_env):
    """Verifies stats KPIs calculations after seeding data."""
    db_path = setup_test_env
    now = datetime.now(timezone.utc)

    # 1. Seed Decisions (10 decisions: 8 kept / successful, 2 reverted)
    for i in range(10):
        d = AIDecision(
            decision_id=f"dec_{i}",
            session_id="sess_123",
            timestamp=now - timedelta(days=i % 3),  # decisions in last 3 days
            provider="openai",
            model_id="gpt-4o",
            task_type=TaskType.code,
            suggestion_summary="summary",
            suggestion_hash=f"hash_{i}",
            user_action=UserAction.accepted,
            outcome=DecisionOutcome.kept if i < 8 else DecisionOutcome.reverted,
            confidence_score=0.9,
            routing_metadata={"latency_ms": 200, "feedback_score": 5.0},
        )
        # Run synchronous insertion in background loop wrapper
        import asyncio
        asyncio.run(insert_decision(db_path, d))

    # 2. Seed Mistakes (2 mistakes)
    m1 = AIMistake(
        mistake_id="mist_1",
        decision_id="dec_8",
        session_id="sess_123",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        failure_type=FailureType.wrong_code,
        failure_summary="syntax error",
        detection_signal="file_revert",
    )
    m2 = AIMistake(
        mistake_id="mist_2",
        decision_id="dec_9",
        session_id="sess_123",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        failure_type=FailureType.broken_build,
        failure_summary="broken build",
        detection_signal="build_failure",
    )
    import asyncio
    asyncio.run(insert_mistake(db_path, m1))
    asyncio.run(insert_mistake(db_path, m2))

    # Refresh profiles cache
    builder = ProviderProfileBuilder(db_path)
    asyncio.run(builder.refresh_provider_profiles())

    # Verify Summary Endpoint
    response = client.get("/stats/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["total_providers"] == 1
    assert data["total_models"] == 1
    assert data["total_decisions"] == 10
    assert data["total_mistakes"] == 2
    assert data["overall_success_rate"] == 0.8
    assert data["overall_quality_score"] > 0.0

    # Verify Providers list
    response = client.get("/stats/providers")
    assert response.status_code == 200
    providers = response.json()
    assert len(providers) == 1
    assert providers[0]["provider_id"] == "openai"
    assert providers[0]["total_decisions"] == 10
    assert providers[0]["success_rate"] == 0.8
    assert providers[0]["number_of_task_types_covered"] == 1

    # Verify Provider Detail
    response = client.get("/stats/providers/openai")
    assert response.status_code == 200
    detail = response.json()
    assert detail["provider_id"] == "openai"
    assert detail["total_decisions"] == 10
    assert detail["success_rate"] == 0.8
    assert detail["decisions_last_7_days"] == 10
    assert detail["decisions_last_30_days"] == 10
    assert len(detail["task_type_breakdown"]) == 1
    assert len(detail["model_breakdown"]) == 1
    assert detail["task_type_breakdown"][0]["name"] == "code"
    assert detail["model_breakdown"][0]["name"] == "gpt-4o"
    assert detail["last_seen_timestamp"] is not None

    # Verify Detail Not Found
    response_missing = client.get("/stats/providers/missing_provider_id")
    assert response_missing.status_code == 404
    assert "no logged metrics" in response_missing.json()["detail"]

    # Verify Task Types list
    response = client.get("/stats/task-types")
    assert response.status_code == 200
    task_types = response.json()
    assert len(task_types) == 1
    assert task_types[0]["task_type"] == "code"
    assert task_types[0]["total_decisions"] == 10
    assert task_types[0]["coverage_quality"] == "medium_coverage"

    # Verify Leaderboard
    response = client.get("/stats/leaderboard")
    assert response.status_code == 200
    leaderboard = response.json()
    assert len(leaderboard) == 1
    assert leaderboard[0]["rank"] == 1
    assert leaderboard[0]["provider_id"] == "openai"
    assert leaderboard[0]["model_name"] == "gpt-4o"
    assert leaderboard[0]["quality_score"] > 0.0


def test_stats_leaderboard_sorting_and_filtering(client, setup_test_env):
    """Verifies that leaderboard correctly filters and sorts providers with tie-breakers."""
    db_path = setup_test_env
    now = datetime.now(timezone.utc)

    # Upsert two different providers directly into the profile cache for leaderboard sorting test
    p_high = ProviderProfile(
        provider="anthropic",
        model_id="claude",
        task_type=TaskType.code,
        total_decisions=50,
        quality_score=0.85,
        profile_confidence="high",
        confidence_factor=1.0,
        cold_start=False,
        last_updated=now,
    )
    p_low = ProviderProfile(
        provider="google",
        model_id="gemini",
        task_type=TaskType.code,
        total_decisions=10,
        quality_score=0.45,
        profile_confidence="low",
        confidence_factor=0.2,
        cold_start=True,
        last_updated=now,
    )

    import asyncio
    asyncio.run(upsert_provider_profile(db_path, p_high))
    asyncio.run(upsert_provider_profile(db_path, p_low))

    # Query leaderboard with code filter
    response = client.get("/stats/leaderboard?task_type=code")
    assert response.status_code == 200
    leaderboard = response.json()
    # Should contain 3 entries (seeded openai from previous test plus anthropic and google)
    assert len(leaderboard) == 3
    # First must be anthropic (claude) since its quality score is 0.85 (highest)
    assert leaderboard[0]["provider_id"] == "anthropic"
    assert leaderboard[0]["model_name"] == "claude"
    assert leaderboard[0]["rank"] == 1
