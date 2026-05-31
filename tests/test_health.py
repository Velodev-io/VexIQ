"""Integration tests for VexIQ API endpoints.

Verifies that the application starts up, manages lifespan database creation, 
and serves root '/' and '/health' responses correctly.
"""

import os
import pytest
from fastapi.testclient import TestClient

from vexiq.config import get_settings
from vexiq.main import app


@pytest.fixture(scope="module", autouse=True)
def setup_test_env(tmp_path_factory):
    """Generates settings overrides using a temporary database path and environment injection."""
    tmp_db_dir = tmp_path_factory.mktemp("vexiq_test_health")
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


def test_root_endpoint(client):
    """Tests the root endpoint descriptor."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "vexiq"
    assert data["version"] == "0.1.0"
    assert "description" in data


def test_health_endpoint(client, setup_test_env):
    """Tests the health check details."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "vexiq"
    assert data["db_initialized"] is True
    assert data["db_path"] == setup_test_env
    assert "table_counts" in data
    assert data["table_counts"]["ai_decisions"] == 0
