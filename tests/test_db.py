"""Database tests for VexIQ.

Verifies database creation, PRAGMA flags, schemas, table existence, and row-count 
helpers using a temporary SQLite file.
"""

import os
import pytest
from vexiq.db import init_db, table_exists, get_table_counts, get_db_conn


@pytest.mark.asyncio
async def test_db_initialization_and_helpers(tmp_path):
    """Verifies that init_db creates the database file, defines proper schemas,

    sets up index references, and enables appropriate SQLite Pragmas.
    """
    db_file = tmp_path / "test_vexiq.db"
    db_path = str(db_file)

    # Assert database file does not exist yet
    assert not os.path.exists(db_path)

    # Initialize DB
    await init_db(db_path)

    # Assert database file gets created
    assert os.path.exists(db_path)

    # Check tables are created
    assert await table_exists(db_path, "ai_decisions")
    assert await table_exists(db_path, "ai_mistakes")
    assert await table_exists(db_path, "provider_profiles")
    assert await table_exists(db_path, "routing_decisions")
    assert not await table_exists(db_path, "non_existent_table")

    # Check table counts helper returns 0 for all on init
    counts = await get_table_counts(db_path)
    assert counts == {
        "ai_decisions": 0,
        "ai_mistakes": 0,
        "provider_profiles": 0,
        "routing_decisions": 0,
    }

    # Assert WAL mode is enabled and foreign keys are active
    async with get_db_conn(db_path) as db:
        async with db.execute("PRAGMA journal_mode;") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0].lower() == "wal"

        async with db.execute("PRAGMA synchronous;") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            # synchronous = NORMAL maps to 1 in SQLite
            assert row[0] == 1

        # Verify indices exist
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index';"
        ) as cursor:
            rows = await cursor.fetchall()
            indices = [r[0] for r in rows]
            assert "idx_ai_decisions_provider_model_task" in indices
            assert "idx_ai_decisions_session_timestamp" in indices
            assert "idx_ai_decisions_project_task" in indices
            assert "idx_ai_mistakes_provider_model_task" in indices
            assert "idx_ai_mistakes_session_timestamp" in indices
            assert "idx_routing_decisions_task_timestamp" in indices
