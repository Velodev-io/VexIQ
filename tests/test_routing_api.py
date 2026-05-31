"""Integration tests for the VexIQ Routing API endpoints.

Verifies HTTP response codes, request query validations, candidates inclusion
toggles, and fallback serialization.
"""

import os
import pytest
from fastapi.testclient import TestClient

from vexiq.config import get_settings
from vexiq.main import app
from vexiq.core.provider_profile import ProviderProfileBuilder


@pytest.fixture(scope="module", autouse=True)
def setup_test_env(tmp_path_factory):
    """Generates settings overrides using a temporary database path and environment injection."""
    tmp_db_dir = tmp_path_factory.mktemp("vexiq_test_routing_api")
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


def test_get_recommendations_cold_start(client):
    """Verifies GET /recommendations returns cold-start fallback when database is empty."""
    response = client.get("/recommendations?task_type=chat")
    assert response.status_code == 200
    data = response.json()
    assert data["task_type"] == "chat"
    assert data["fallback_used"] is True
    assert data["decision_source"] == "cold_start_default"
    assert data["fallback_reason"] == "no_profile_data"
    assert len(data["ranked_candidates"]) > 0
    assert data["recommended_provider"] == "openai"
    assert data["recommended_model"] == "gpt-4.1"


def test_get_recommendations_invalid_task_type(client):
    """Verifies GET /recommendations returns 422 for unsupported task types."""
    response = client.get("/recommendations?task_type=invalid_task_type")
    assert response.status_code == 422
    data = response.json()
    assert "detail" in data
    # Assert location details point to query param
    assert data["detail"][0]["loc"] == ["query", "task_type"]


def test_get_recommendations_exclude_candidates(client):
    """Verifies include_candidates=false query parameter clears candidates lists in response."""
    response = client.get("/recommendations?task_type=code&include_candidates=false")
    assert response.status_code == 200
    data = response.json()
    assert data["ranked_candidates"] == []
    assert data["competing_providers"] == []


def test_get_recommendations_with_seeded_data(client, setup_test_env):
    """Verifies routing logic with seeded decision logs."""
    db_path = setup_test_env

    # Seed 5 decisions via decisions endpoint
    for i in range(5):
        payload = {
            "session_id": "sess_api",
            "provider": "anthropic",
            "model_id": "claude-3-5-sonnet",
            "task_type": "code",
            "suggestion_summary": f"code suggestion {i}",
            "suggestion_hash": f"hash_api_{i}",
            "user_action": "accepted",
        }
        res_post = client.post("/decisions", json=payload)
        assert res_post.status_code == 201
        decision_id = res_post.json()["decision_id"]

        # Patch outcome to kept so success rate = 1.0
        res_patch = client.patch(
            f"/decisions/{decision_id}/outcome", json={"outcome": "kept"}
        )
        assert res_patch.status_code == 200

    # Refresh profiles cache
    builder = ProviderProfileBuilder(db_path)
    # We run it synchronously inside our test via an event loop wrapper (pytest-asyncio automatically handles async fixtures/tests but here we call it from sync test context)
    import asyncio
    asyncio.run(builder.refresh_provider_profiles())

    # Query routing API
    response = client.get("/recommendations?task_type=code")
    assert response.status_code == 200
    data = response.json()

    assert data["task_type"] == "code"
    # Should use the seeded provider
    assert data["recommended_provider"] == "anthropic"
    assert data["recommended_model"] == "claude-3-5-sonnet"
    # Although it is used, it should be marked as fallback because total_decisions = 5, which is low confidence
    assert data["fallback_used"] is True
    assert data["decision_source"] == "low_confidence_fallback"
    assert data["fallback_reason"] == "insufficient_history"
