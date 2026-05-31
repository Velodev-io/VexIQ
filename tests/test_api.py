"""Tests for the VexIQ FastAPI endpoints.

Utilizes httpx/FastAPI TestClient to verify routing requests, request/response models validation, 
decisions/mistakes recording, and stats summaries.
"""

import os
import pytest
from fastapi.testclient import TestClient

from vexiq.config import get_settings
from vexiq.main import app


@pytest.fixture(scope="module", autouse=True)
def setup_test_env(tmp_path_factory):
    """Generates settings overrides using a temporary database path and environment injection."""
    tmp_db_dir = tmp_path_factory.mktemp("vexiq_test_api")
    db_path = str(tmp_db_dir / "vexiq.db")

    # Store old variables if they exist
    old_db_path = os.environ.get("VEXIQ_DB_PATH")
    old_port = os.environ.get("VEXIQ_PORT")

    os.environ["VEXIQ_DB_PATH"] = db_path
    os.environ["VEXIQ_PORT"] = "9999"
    get_settings.cache_clear()

    yield db_path

    # Restore
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


def test_create_decision_success(client):
    """Verifies that POST /decisions registers new events with 201, and duplicates with 200."""
    payload = {
        "session_id": "session_api_1",
        "provider": "openai",
        "model_id": "gpt-4o",
        "task_type": "code_edit",
        "suggestion_summary": "Fix syntax error",
        "suggestion_hash": "hash_api_1",
        "user_action": "accepted",
        "confidence_score": 0.85,
        "routing_metadata": {"key": "val"},
    }
    response = client.post("/decisions", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert "decision_id" in data
    assert data["session_id"] == "session_api_1"
    assert data["outcome"] == "unknown"

    decision_id = data["decision_id"]

    # Try creating the identical decision (should trigger deduplication and return 200)
    response_dup = client.post("/decisions", json=payload)
    assert response_dup.status_code == 200
    data_dup = response_dup.json()
    assert data_dup["decision_id"] == decision_id


def test_get_decision(client):
    """Verifies that GET /decisions/{id} returns 200 if found, and 404 if missing."""
    payload = {
        "session_id": "session_api_2",
        "provider": "openai",
        "model_id": "gpt-4o",
        "task_type": "command",
        "suggestion_summary": "Run tests",
        "suggestion_hash": "hash_api_2",
        "user_action": "accepted",
    }
    response = client.post("/decisions", json=payload)
    decision_id = response.json()["decision_id"]

    # Test GET found
    response_get = client.get(f"/decisions/{decision_id}")
    assert response_get.status_code == 200
    assert response_get.json()["decision_id"] == decision_id

    # Test GET missing
    response_missing = client.get("/decisions/missing_id")
    assert response_missing.status_code == 404
    assert response_missing.json()["detail"] == "Decision with ID 'missing_id' not found"


def test_patch_outcome(client):
    """Verifies that PATCH /decisions/{id}/outcome logs outcome status with 200, and 404 if missing."""
    payload = {
        "session_id": "session_api_3",
        "provider": "openai",
        "model_id": "gpt-4o",
        "task_type": "config",
        "suggestion_summary": "Set port",
        "suggestion_hash": "hash_api_3",
        "user_action": "modified",
    }
    response = client.post("/decisions", json=payload)
    decision_id = response.json()["decision_id"]

    # Test PATCH outcome
    patch_payload = {"outcome": "reverted"}
    response_patch = client.patch(
        f"/decisions/{decision_id}/outcome", json=patch_payload
    )
    assert response_patch.status_code == 200
    data_patch = response_patch.json()
    assert data_patch["outcome"] == "reverted"
    assert data_patch["outcome_recorded_at"] is not None

    # Test PATCH missing
    response_missing = client.patch(
        "/decisions/missing_id/outcome", json=patch_payload
    )
    assert response_missing.status_code == 404
    assert response_missing.json()["detail"] == "Decision with ID 'missing_id' not found"


def test_list_decisions(client):
    """Verifies list query parameters return array logs."""
    response = client.get("/decisions?limit=5")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0


def test_create_mistake_success_and_dedup(client):
    """Verifies that POST /mistakes creates a mistake and deduplicates subsequent submissions."""
    payload = {
        "session_id": "session_mistake_1",
        "provider": "openai",
        "model_id": "gpt-4o",
        "task_type": "code_edit",
        "failure_type": "wrong_code",
        "failure_summary": "Bad syntax output",
        "severity": "medium",
        "auto_detected": False,
        "detection_signal": "EXPLICIT_REPORT",
    }
    response = client.post("/mistakes", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert "mistake_id" in data
    assert data["session_id"] == "session_mistake_1"
    assert data["failure_type"] == "wrong_code"

    mistake_id = data["mistake_id"]

    # Try duplicate post (should return 200 and the same mistake_id)
    response_dup = client.post("/mistakes", json=payload)
    assert response_dup.status_code == 200
    data_dup = response_dup.json()
    assert data_dup["mistake_id"] == mistake_id


def test_create_mistake_with_nonexistent_decision(client):
    """Verifies that POST /mistakes returns 400 when referenced decision_id does not exist."""
    payload = {
        "decision_id": "nonexistent_dec_id",
        "session_id": "session_mistake_2",
        "provider": "openai",
        "model_id": "gpt-4o",
        "task_type": "code_edit",
        "failure_type": "wrong_code",
        "failure_summary": "Bad output",
        "severity": "medium",
        "auto_detected": False,
        "detection_signal": "EXPLICIT_REPORT",
    }
    response = client.post("/mistakes", json=payload)
    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]


def test_create_mistake_invalid_payload(client):
    """Verifies that POST /mistakes returns 422 when fields are missing or invalid."""
    # missing session_id
    payload = {
        "provider": "openai",
        "model_id": "gpt-4o",
        "task_type": "code_edit",
        "failure_type": "wrong_code",
        "failure_summary": "",  # Empty string (violates non-empty validator)
    }
    response = client.post("/mistakes", json=payload)
    assert response.status_code == 422


def test_flag_mistake_success(client):
    """Verifies that POST /mistakes/flag successfully registers flagging payload."""
    payload = {
        "session_id": "session_flag_1",
        "provider": "openai",
        "model_id": "gpt-4o",
        "task_type": "code_edit",
        "failure_summary": "Hallucinated import",
        "correction_summary": "Used proper import name",
    }
    response = client.post("/mistakes/flag", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert "mistake_id" in data
    assert data["session_id"] == "session_flag_1"
    assert data["failure_type"] == "explicit_rejection"
    assert data["correction_made"] is True
    assert data["auto_detected"] is False
    assert data["detection_signal"] == "EXPLICIT_FLAG"


def test_get_mistake(client):
    """Verifies GET /mistakes/{id} returns 200 if found, and 404 if missing."""
    payload = {
        "session_id": "session_get_mistake",
        "provider": "openai",
        "model_id": "gpt-4o",
        "task_type": "code_edit",
        "failure_type": "wrong_code",
        "failure_summary": "Bad indent",
    }
    response = client.post("/mistakes", json=payload)
    mistake_id = response.json()["mistake_id"]

    # Test GET found
    response_get = client.get(f"/mistakes/{mistake_id}")
    assert response_get.status_code == 200
    assert response_get.json()["mistake_id"] == mistake_id

    # Test GET missing
    response_missing = client.get("/mistakes/missing_mistake_id")
    assert response_missing.status_code == 404
    assert (
        response_missing.json()["detail"]
        == "Mistake with ID 'missing_mistake_id' not found"
    )


def test_list_mistakes(client):
    """Verifies GET /mistakes list endpoint return array logs."""
    response = client.get("/mistakes?limit=5")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0

