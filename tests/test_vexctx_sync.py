"""Tests for the VexIQ VexCTX Sync Engine core module.

Verifies incremental synchronization of decisions, mistakes, and provider profiles,
asserting proper serialization, HTTP payload formats, checkpoint updates, failure handling,
and API manual sync triggering.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import httpx

from vexiq.config import Settings
from vexiq.models import (
    AIDecision,
    AIMistake,
    ProviderProfile,
    TaskType,
    UserAction,
    DecisionOutcome,
    FailureType,
    Severity,
)
from vexiq.db import (
    init_db,
    insert_decision,
    insert_mistake,
    upsert_provider_profile,
    get_sync_checkpoint,
)
from vexiq.core.vexctx_sync import VexCTXSyncEngine, SyncError


@pytest_asyncio.fixture
async def setup_test_db(tmp_path):
    """Initializes an isolated SQLite database file for testing."""
    db_file = tmp_path / "vexiq_test_sync.db"
    db_path = str(db_file)
    await init_db(db_path)
    return db_path


@pytest.fixture
def test_settings():
    """Returns a Settings object configured for local mock tests."""
    return Settings(
        vexiq_db_path="~/.vexiq/vexiq_test.db",
        vexctx_base_url="http://mock-vexctx:8765",
        vexiq_vexctx_sync_enabled=True,
        vexctx_api_key="test-api-key-123",
        vexiq_sync_batch_size=5,
        vexiq_sync_timeout_seconds=2.0,
        vexiq_sync_retry_attempts=2,
    )


@pytest.mark.asyncio
async def test_sync_decisions_success(setup_test_db, test_settings):
    """Verifies that unsynced decisions are serialized, sent, and the checkpoint is advanced."""
    db_path = setup_test_db
    engine = VexCTXSyncEngine(db_path, test_settings)

    # 1. Seed two decisions in local SQLite
    now = datetime.now(timezone.utc)
    d1 = AIDecision(
        decision_id="dec_1",
        session_id="sess_1",
        timestamp=now - timedelta(minutes=2),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="Fix bug 1",
        suggestion_hash="hash_1",
        user_action=UserAction.accepted,
        confidence_score=0.95,
    )
    d2 = AIDecision(
        decision_id="dec_2",
        session_id="sess_1",
        timestamp=now - timedelta(minutes=1),
        provider="anthropic",
        model_id="claude-3-5-sonnet",
        task_type=TaskType.command,
        suggestion_summary="Run migrations",
        suggestion_hash="hash_2",
        user_action=UserAction.modified,
        modification_summary="Clean DB migrations",
        confidence_score=0.85,
    )

    await insert_decision(db_path, d1)
    await insert_decision(db_path, d2)

    # Mock the HTTP POST client call
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = httpx.Response(
            status_code=200,
            request=httpx.Request("POST", "http://mock-vexctx:8765/sync")
        )
        mock_post.return_value = mock_response

        # 2. Call the decisions sync
        synced_count = await engine.sync_decisions(limit=10)
        assert synced_count == 2

        # Verify POST payload and headers
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "http://mock-vexctx:8765/sync"
        assert kwargs["headers"]["Authorization"] == "Bearer test-api-key-123"
        payload = kwargs["json"]
        assert payload["source"] == "vexiq"
        assert payload["record_type"] == "decisions"
        assert len(payload["records"]) == 2
        
        # Verify serialized values
        rec1 = payload["records"][0]
        assert rec1["decision_id"] == "dec_1"
        assert rec1["provider_used"] == "openai"
        assert rec1["model_used"] == "gpt-4o"
        assert rec1["action_taken"] == "accepted"
        assert rec1["context_snapshot"]["suggestion_summary"] == "Fix bug 1"
        assert rec1["context_snapshot"]["confidence_score"] == 0.95

        rec2 = payload["records"][1]
        assert rec2["decision_id"] == "dec_2"
        assert rec2["provider_used"] == "anthropic"
        assert rec2["model_used"] == "claude-3-5-sonnet"
        assert rec2["action_taken"] == "modified"
        assert rec2["context_snapshot"]["suggestion_summary"] == "Run migrations"
        assert rec2["context_snapshot"]["modification_summary"] == "Clean DB migrations"

        # Verify checkpoint advances
        checkpoint = await get_sync_checkpoint(db_path, "decisions")
        assert checkpoint is not None
        # last_synced_id should match the last record in the batch
        assert checkpoint[1] == "dec_2"

        # 3. Call sync_decisions again (should sync 0 since checkpoint advanced)
        mock_post.reset_mock()
        again_count = await engine.sync_decisions(limit=10)
        assert again_count == 0
        mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_sync_mistakes_success(setup_test_db, test_settings):
    """Verifies mistakes sync is serializing negative feedback fields correctly and advancing cursor."""
    db_path = setup_test_db
    engine = VexCTXSyncEngine(db_path, test_settings)

    now = datetime.now(timezone.utc)
    m1 = AIMistake(
        mistake_id="mist_1",
        decision_id="dec_1",
        session_id="sess_1",
        timestamp=now - timedelta(minutes=1),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_code,
        failure_summary="IndentationError",
        correction_made=True,
        correction_summary="Fixed indentation tabs",
        severity=Severity.medium,
        auto_detected=True,
        detection_signal="HEAVY_EDIT",
        outcome_type="edited_further",
        user_corrected=True,
        correction_detail="Fixed indentation tabs",
        feedback_signal=0.5,
    )
    await insert_mistake(db_path, m1)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = httpx.Response(
            status_code=200,
            request=httpx.Request("POST", "http://mock-vexctx:8765/sync")
        )
        mock_post.return_value = mock_response

        synced_count = await engine.sync_mistakes()
        assert synced_count == 1

        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["record_type"] == "mistakes"
        assert len(payload["records"]) == 1

        rec = payload["records"][0]
        assert rec["mistake_id"] == "mist_1"
        assert rec["decision_id"] == "dec_1"
        assert rec["outcome_type"] == "wrong_code"
        assert rec["user_corrected"] is True
        assert rec["correction_detail"] == "Fixed indentation tabs"
        assert rec["feedback_signal"] == 1.0
        assert rec["failure_summary"] == "IndentationError"
        assert rec["detection_signal"] == "HEAVY_EDIT"

        checkpoint = await get_sync_checkpoint(db_path, "mistakes")
        assert checkpoint is not None
        assert checkpoint[1] == "mist_1"


@pytest.mark.asyncio
async def test_sync_provider_profiles_success(setup_test_db, test_settings):
    """Verifies that provider profile aggregations are serialized correctly."""
    db_path = setup_test_db
    engine = VexCTXSyncEngine(db_path, test_settings)

    p1 = ProviderProfile(
        provider="openai",
        model_id="gpt-4o",
        provider_id="openai",
        model_name="gpt-4o",
        task_type=TaskType.code_edit,
        total_decisions=15,
        successful_decisions=12,
        mistake_count=3,
        success_rate=0.8,
        mistake_rate=0.2,
        correction_rate=0.1,
        quality_score=0.75,
        profile_confidence="medium",
        confidence_factor=0.6,
        cold_start=False,
        last_seen_at=datetime.now(timezone.utc),
        last_updated=datetime.now(timezone.utc),
    )
    await upsert_provider_profile(db_path, p1)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = httpx.Response(
            status_code=200,
            request=httpx.Request("POST", "http://mock-vexctx:8765/sync")
        )
        mock_post.return_value = mock_response

        synced_count = await engine.sync_provider_profiles()
        assert synced_count == 1

        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["record_type"] == "provider_profiles"
        assert len(payload["records"]) == 1

        rec = payload["records"][0]
        assert rec["provider_id"] == "openai"
        assert rec["model_name"] == "gpt-4o"
        assert rec["task_type"] == "code_edit"
        assert rec["quality_score"] == 0.75
        assert rec["profile_confidence"] == "medium"
        assert rec["confidence_factor"] == 0.6
        assert rec["success_rate"] == 0.8
        assert rec["mistake_rate"] == 0.2
        assert rec["correction_rate"] == 0.1
        assert rec["total_decisions"] == 15
        assert rec["cold_start"] is False

        checkpoint = await get_sync_checkpoint(db_path, "provider_profiles")
        assert checkpoint is not None
        assert checkpoint[1] == "openai|gpt-4o|code_edit"


@pytest.mark.asyncio
async def test_sync_failure_retry_and_no_checkpoint_advance(setup_test_db, test_settings):
    """Verifies that if HTTP sync fails after all retries, SyncError is raised and checkpoint does not advance."""
    db_path = setup_test_db
    engine = VexCTXSyncEngine(db_path, test_settings)

    now = datetime.now(timezone.utc)
    d = AIDecision(
        decision_id="dec_fail_1",
        session_id="sess_fail",
        timestamp=now,
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        suggestion_summary="summary",
        suggestion_hash="hash_fail",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, d)

    # Mock HTTP client to raise error
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(SyncError):
            await engine.sync_decisions()

        # Check HTTP mock was called the expected number of retry attempts
        assert mock_post.call_count == test_settings.vexiq_sync_retry_attempts

        # Verify checkpoint has not advanced (should be None)
        checkpoint = await get_sync_checkpoint(db_path, "decisions")
        assert checkpoint is None


@pytest.mark.asyncio
async def test_sync_empty_records(setup_test_db, test_settings):
    """Verifies that trying to sync empty tables yields 0 cleanly without making network requests."""
    db_path = setup_test_db
    engine = VexCTXSyncEngine(db_path, test_settings)

    with patch("httpx.AsyncClient.post") as mock_post:
        count = await engine.sync_decisions()
        assert count == 0
        mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_get_sync_status_and_reset(setup_test_db, test_settings):
    """Verifies status reporting and reset endpoints."""
    db_path = setup_test_db
    engine = VexCTXSyncEngine(db_path, test_settings)

    # Initial status should be all None
    status = await engine.get_sync_status()
    assert status["decisions"]["last_synced_id"] is None
    assert status["mistakes"]["last_synced_id"] is None

    # Insert a fake checkpoint
    from vexiq.db import update_sync_checkpoint
    await update_sync_checkpoint(db_path, "decisions", "2026-05-31T00:00:00Z", "dec_fake")

    status = await engine.get_sync_status()
    assert status["decisions"]["last_synced_id"] == "dec_fake"

    # Reset checkpoint
    await engine.reset_sync_checkpoint("decisions")
    status = await engine.get_sync_status()
    assert status["decisions"]["last_synced_id"] is None


def test_api_sync_endpoints():
    """Verifies that the /sync/ FastAPI endpoints invoke the SyncEngine correctly."""
    from fastapi.testclient import TestClient
    from vexiq.main import app

    client = TestClient(app)
    
    with patch("vexiq.core.vexctx_sync.VexCTXSyncEngine.sync_all") as mock_sync_all:
        mock_sync_all.return_value = {
            "decisions": {"status": "success", "synced_count": 2, "error": None},
            "mistakes": {"status": "success", "synced_count": 1, "error": None},
            "provider_profiles": {"status": "success", "synced_count": 0, "error": None},
        }
        
        response = client.post("/sync/all?limit_per_type=50")
        assert response.status_code == 200
        data = response.json()
        assert data["decisions"]["synced_count"] == 2
        mock_sync_all.assert_called_once_with(50)

    with patch("vexiq.core.vexctx_sync.VexCTXSyncEngine.sync_decisions") as mock_sync_decisions:
        mock_sync_decisions.return_value = 5
        response = client.post("/sync/decisions?limit=10")
        assert response.status_code == 200
        assert response.json() == {"status": "success", "synced_count": 5}
        mock_sync_decisions.assert_called_once_with(10)

    with patch("vexiq.core.vexctx_sync.VexCTXSyncEngine.sync_mistakes") as mock_sync_mistakes:
        mock_sync_mistakes.return_value = 1
        response = client.post("/sync/mistakes?limit=5")
        assert response.status_code == 200
        assert response.json() == {"status": "success", "synced_count": 1}
        mock_sync_mistakes.assert_called_once_with(5)

    with patch("vexiq.core.vexctx_sync.VexCTXSyncEngine.sync_provider_profiles") as mock_sync_profiles:
        mock_sync_profiles.return_value = 0
        response = client.post("/sync/profiles?limit=5")
        assert response.status_code == 200
        assert response.json() == {"status": "success", "synced_count": 0}
        mock_sync_profiles.assert_called_once_with(5)

    with patch("vexiq.core.vexctx_sync.VexCTXSyncEngine.get_sync_status") as mock_status:
        mock_status.return_value = {"decisions": {"last_synced_id": "dec_123"}}
        response = client.get("/sync/status")
        assert response.status_code == 200
        assert response.json() == {"decisions": {"last_synced_id": "dec_123"}}
        mock_status.assert_called_once()

    with patch("vexiq.core.vexctx_sync.VexCTXSyncEngine.reset_sync_checkpoint") as mock_reset:
        response = client.post("/sync/reset?record_type=decisions")
        assert response.status_code == 200
        assert "Checkpoint reset complete" in response.json()["detail"]
        mock_reset.assert_called_once_with("decisions")

