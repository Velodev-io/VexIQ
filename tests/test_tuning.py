"""Tests for the VexIQ threshold parameter tuning and simulation engine.

Verifies dynamic threshold overrides, SQLite manual labeling persistence,
metric parsing helper, and simulation calculations.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from vexiq.config import Settings
from vexiq.models import TaskType, AIMistake, FailureType, Severity
from vexiq.db import (
    init_db,
    get_event_label,
    set_event_label,
    get_labeled_events,
    insert_mistake,
)
from vexiq.core.detection_signals import _get_thresholds
from vexiq.core.tune_thresholds import parse_metrics, cmd_simulate


@pytest_asyncio.fixture
async def setup_test_db(tmp_path):
    """Initializes an isolated SQLite database file for testing."""
    db_file = tmp_path / "vexiq_test_tuning.db"
    db_path = str(db_file)
    await init_db(db_path)
    return db_path


@pytest.mark.asyncio
async def test_dynamic_thresholds_override(setup_test_db):
    """Verifies that _get_thresholds retrieves config values from settings dynamically."""
    custom_thresholds = {
        "code": {
            "heavy_edit_threshold": 0.44,
            "manual_rewrite_threshold": 0.66,
            "time_window_minutes": 12,
            "immediate_retry_max_seconds": 99,
            "test_fix_max_minutes": 11,
        }
    }
    
    settings_override = Settings(
        vexiq_db_path=setup_test_db,
        vexiq_task_type_thresholds=custom_thresholds
    )

    with patch("vexiq.core.detection_signals.get_settings", return_value=settings_override):
        thresholds = _get_thresholds("code")
        assert thresholds["heavy_edit_threshold"] == 0.44
        assert thresholds["manual_rewrite_threshold"] == 0.66
        assert thresholds["immediate_retry_max_seconds"] == 99
        assert thresholds["test_fix_max_minutes"] == 11


@pytest.mark.asyncio
async def test_labeled_events_persistence(setup_test_db):
    """Verifies that set_event_label, get_event_label, and get_labeled_events operate correctly."""
    db_path = setup_test_db

    # Initially empty
    label = await get_event_label(db_path, "event_1")
    assert label is None

    # Set label
    await set_event_label(db_path, "event_1", "mistake", "true_mistake")
    label = await get_event_label(db_path, "event_1")
    assert label == "true_mistake"

    # Set another
    await set_event_label(db_path, "event_2", "decision", "missed_mistake")
    
    all_events = await get_labeled_events(db_path)
    assert len(all_events) == 2
    assert all_events[0]["event_id"] in ("event_1", "event_2")


def test_parse_metrics():
    """Verifies parsing of numeric metrics from various summary texts."""
    # Heavy edit parsing
    assert parse_metrics("heavy_edit", "Heavy edit detected (edit ratio: 35.50%).") == 0.355
    assert parse_metrics("heavy_edit", "Random text") is None

    # Manual rewrite parsing
    assert parse_metrics("manual_rewrite", "Manual rewrite detected (rewrite ratio: 82.00%).") == 0.82
    
    # Immediate retry parsing
    assert parse_metrics("immediate_retry", "Immediate retry of task after 45.5s.") == 45.5
    assert parse_metrics("immediate_retry", "Immediate retry of task after 120s.") == 120.0


@pytest.mark.asyncio
async def test_simulation_execution(setup_test_db):
    """Verifies simulation logic correctly groups, filters, and computes precision/recall stats."""
    db_path = setup_test_db

    # Seed mistakes in SQLite
    m1 = AIMistake(
        mistake_id="m_1",
        decision_id="dec_1",
        session_id="sess_1",
        timestamp=datetime.now(timezone.utc),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_code,
        failure_summary="Heavy edit detected on file.py (edit ratio: 35.00%).",
        severity=Severity.medium,
        auto_detected=True,
        detection_signal="heavy_edit",
    )
    m2 = AIMistake(
        mistake_id="m_2",
        decision_id="dec_1",
        session_id="sess_1",
        timestamp=datetime.now(timezone.utc),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code_edit,
        failure_type=FailureType.wrong_code,
        failure_summary="Heavy edit detected on file.py (edit ratio: 55.00%).",
        severity=Severity.high,
        auto_detected=True,
        detection_signal="heavy_edit",
    )
    
    await insert_mistake(db_path, m1)
    await insert_mistake(db_path, m2)

    # Label them: m_1 is a true mistake, m_2 is a false positive
    await set_event_label(db_path, "m_1", "mistake", "true_mistake")
    await set_event_label(db_path, "m_2", "mistake", "false_positive")

    # Run simulation with heavy edit threshold of 0.30 (both trigger)
    # TP = 1 (m1), FP = 1 (m2), FN = 0. Precision = 50%, Recall = 100%
    with patch("builtins.print") as mock_print:
        await cmd_simulate(db_path, heavy_edit=0.30, manual_rewrite=0.50, retry_seconds=120.0)
        # Check printed results
        printed_calls = [c[0][0] for c in mock_print.call_args_list if c[0]]
        # Confirm summary contains expected numbers
        assert any("True Positives (Triggered Mistakes):      1" in p for p in printed_calls)
        assert any("False Positives (Triggered Non-Mistakes): 1" in p for p in printed_calls)
        assert any("Precision:                                50.0%" in p for p in printed_calls)
        assert any("Recall Proxy:                             100.0%" in p for p in printed_calls)

    # Run simulation with heavy edit threshold of 0.40 (only m_2 triggers)
    # TP = 0 (m1 fails to trigger), FP = 1 (m2 triggers), FN = 1 (m1 was true mistake but missed).
    # Precision = 0%, Recall = 0%
    with patch("builtins.print") as mock_print:
        await cmd_simulate(db_path, heavy_edit=0.40, manual_rewrite=0.50, retry_seconds=120.0)
        printed_calls = [c[0][0] for c in mock_print.call_args_list if c[0]]
        assert any("True Positives (Triggered Mistakes):      0" in p for p in printed_calls)
        assert any("False Positives (Triggered Non-Mistakes): 1" in p for p in printed_calls)
        assert any("False Negatives (Missed Mistakes):        1" in p for p in printed_calls)
