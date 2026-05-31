"""Tests for VexIQ Mistake Auto-Detection Signals.

Verifies revert, heavy edit, build error, manual rewrite detectors,
closest preceding decision resolution, and batch log ingestion.
"""

import os
import json
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta

from vexiq.models import (
    AIDecision,
    AIMistake,
    TaskType,
    UserAction,
    FailureType,
    Severity,
    DecisionOutcome,
)
from vexiq.db import init_db, insert_decision, get_decision_by_id, list_recent_mistakes
from vexiq.core.detection_signals import (
    resolve_decision_id,
    detect_file_revert,
    detect_heavy_edit,
    detect_build_error,
    detect_manual_rewrite,
    detect_immediate_retry,
    detect_test_fix_loop,
    ingest_signals_from_vexon_logs,
)


@pytest_asyncio.fixture
async def setup_test_db(tmp_path):
    """Initializes an isolated SQLite database file for testing."""
    db_file = tmp_path / "vexiq_test_signals.db"
    db_path = str(db_file)
    await init_db(db_path)
    return db_path


@pytest.mark.asyncio
async def test_resolve_decision_id(setup_test_db):
    """Tests that resolve_decision_id finds the closest preceding decision within the window."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    # 1. Seed two decisions in the same session and task type
    # Dec 1: 15 minutes ago
    dec1 = AIDecision(
        decision_id="dec_old",
        session_id="session_1",
        timestamp=now - timedelta(minutes=15),
        provider="anthropic",
        model_id="claude-3",
        task_type=TaskType.code_edit,
        suggestion_summary="Older edit",
        suggestion_hash="hash_old",
        user_action=UserAction.accepted,
    )
    # Dec 2: 5 minutes ago
    dec2 = AIDecision(
        decision_id="dec_recent",
        session_id="session_1",
        timestamp=now - timedelta(minutes=5),
        provider="anthropic",
        model_id="claude-3",
        task_type=TaskType.code_edit,
        suggestion_summary="Recent edit",
        suggestion_hash="hash_recent",
        user_action=UserAction.accepted,
    )
    # Dec 3: 2 minutes in the FUTURE (should not match)
    dec3 = AIDecision(
        decision_id="dec_future",
        session_id="session_1",
        timestamp=now + timedelta(minutes=2),
        provider="anthropic",
        model_id="claude-3",
        task_type=TaskType.code_edit,
        suggestion_summary="Future edit",
        suggestion_hash="hash_future",
        user_action=UserAction.accepted,
    )

    await insert_decision(db_path, dec1)
    await insert_decision(db_path, dec2)
    await insert_decision(db_path, dec3)

    # Resolve at event time (now)
    # Should resolve to the closest preceding decision (dec_recent, 5 mins ago)
    resolved = await resolve_decision_id(
        session_id="session_1",
        task_type=TaskType.code_edit.value,
        timestamp=now,
        window_minutes=10,
        db_path=db_path,
    )
    assert resolved == "dec_recent"

    # If the window is too small (e.g. 2 minutes), it shouldn't find dec_recent (5 mins ago)
    resolved_tight = await resolve_decision_id(
        session_id="session_1",
        task_type=TaskType.code_edit.value,
        timestamp=now,
        window_minutes=2,
        db_path=db_path,
    )
    assert resolved_tight is None

    # If we resolve 20 minutes ago, only dec_old could match, but it's outside the window,
    # and dec_recent/dec_future are in the future of that timestamp.
    resolved_past = await resolve_decision_id(
        session_id="session_1",
        task_type=TaskType.code_edit.value,
        timestamp=now - timedelta(minutes=20),
        window_minutes=10,
        db_path=db_path,
    )
    assert resolved_past is None


@pytest.mark.asyncio
async def test_detect_file_revert(setup_test_db):
    """Tests file revert detection, outcome updates, and mapping fields."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    # Seed decision
    dec = AIDecision(
        decision_id="dec_revert_test",
        session_id="session_revert",
        timestamp=now - timedelta(minutes=10),
        provider="openai",
        model_id="gpt-4",
        task_type=TaskType.code_edit,
        suggestion_summary="Code change to revert",
        suggestion_hash="hash_revert",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec)

    # Revert event
    revert_context = {"lines_reverted": 35}
    mistake = await detect_file_revert(
        session_id="session_revert",
        file_path="main.py",
        revert_context=revert_context,
        timestamp=now,
        db_path=db_path,
    )

    assert mistake is not None
    assert mistake.decision_id == "dec_revert_test"
    assert mistake.outcome_type == "revert"
    assert mistake.user_corrected is True
    assert mistake.correction_detail == "file_revert"
    assert abs(mistake.feedback_signal - (-0.49)) < 1e-5
    assert mistake.severity == Severity.high  # > 20 lines
    assert mistake.detection_signal == "file_revert"

    # Verify decision outcome updated
    updated_dec = await get_decision_by_id(db_path, "dec_revert_test")
    assert updated_dec.outcome == DecisionOutcome.reverted


@pytest.mark.asyncio
async def test_detect_heavy_edit(setup_test_db):
    """Tests heavy edit detection thresholds, outcomes, and negative cases."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    dec = AIDecision(
        decision_id="dec_edit_test",
        session_id="session_edit",
        timestamp=now - timedelta(minutes=4),
        provider="google",
        model_id="gemini",
        task_type=TaskType.code_edit,
        suggestion_summary="Code suggestion",
        suggestion_hash="hash_edit",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec)

    # 1. Edit below threshold (20% changed) -> Should return None
    diff_low = {"added": 10, "removed": 5, "changed": 5, "total_lines": 100}
    mistake_low = await detect_heavy_edit(
        session_id="session_edit",
        file_path="utils.py",
        diff_stats=diff_low,
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_low is None

    # 2. Edit above threshold (40% changed) -> Should log mistake
    diff_high = {"added": 20, "removed": 10, "changed": 10, "total_lines": 100}
    mistake_high = await detect_heavy_edit(
        session_id="session_edit",
        file_path="utils.py",
        diff_stats=diff_high,
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_high is not None
    assert mistake_high.decision_id == "dec_edit_test"
    assert mistake_high.outcome_type == "correction"
    assert mistake_high.user_corrected is True
    assert mistake_high.correction_detail == "heavy_edit"
    assert abs(mistake_high.feedback_signal - (-0.2)) < 1e-5
    assert mistake_high.severity == Severity.medium

    # Verify decision outcome updated
    updated_dec = await get_decision_by_id(db_path, "dec_edit_test")
    assert updated_dec.outcome == DecisionOutcome.edited_further


@pytest.mark.asyncio
async def test_detect_build_error(setup_test_db):
    """Tests build error patterns, severity levels, and failure types."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    # Seed separate decisions with distinct session IDs to prevent database collisions
    dec_syntax = AIDecision(
        decision_id="dec_build_syntax",
        session_id="session_build_1",
        timestamp=now - timedelta(minutes=1),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.command,
        suggestion_summary="Build command 1",
        suggestion_hash="hash_build_1",
        user_action=UserAction.accepted,
    )
    dec_assertion = AIDecision(
        decision_id="dec_build_assertion",
        session_id="session_build_2",
        timestamp=now - timedelta(minutes=1),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.command,
        suggestion_summary="Build command 2",
        suggestion_hash="hash_build_2",
        user_action=UserAction.accepted,
    )
    dec_test = AIDecision(
        decision_id="dec_build_test",
        session_id="session_build_3",
        timestamp=now - timedelta(minutes=1),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.command,
        suggestion_summary="Build command 3",
        suggestion_hash="hash_build_3",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec_syntax)
    await insert_decision(db_path, dec_assertion)
    await insert_decision(db_path, dec_test)

    # 1. Test Syntax Error
    mistake_syntax = await detect_build_error(
        session_id="session_build_1",
        task_type=TaskType.command.value,
        error_log="SyntaxError: invalid syntax in file.py",
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_syntax is not None
    assert mistake_syntax.decision_id == "dec_build_syntax"
    assert mistake_syntax.outcome_type == "failure"
    assert mistake_syntax.user_corrected is False
    assert mistake_syntax.feedback_signal == -0.8
    assert mistake_syntax.severity == Severity.high
    assert mistake_syntax.failure_type == FailureType.broken_build

    # 2. Test Assertion Error
    mistake_assertion = await detect_build_error(
        session_id="session_build_2",
        task_type=TaskType.command.value,
        error_log="AssertionError: some assertion failed",
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_assertion is not None
    assert mistake_assertion.decision_id == "dec_build_assertion"
    assert mistake_assertion.outcome_type == "failure"
    assert mistake_assertion.user_corrected is False
    assert mistake_assertion.feedback_signal == -0.6
    assert mistake_assertion.severity == Severity.medium
    assert mistake_assertion.failure_type == FailureType.test_failure

    # 3. Test Test Failure Specifically
    mistake_test = await detect_build_error(
        session_id="session_build_3",
        task_type=TaskType.command.value,
        error_log="failed test: test_method failed",
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_test is not None
    assert mistake_test.decision_id == "dec_build_test"
    assert mistake_test.outcome_type == "failure"
    assert mistake_test.user_corrected is False
    assert mistake_test.feedback_signal == -0.7
    assert mistake_test.severity == Severity.high
    assert mistake_test.failure_type == FailureType.test_failure


@pytest.mark.asyncio
async def test_detect_manual_rewrite(setup_test_db):
    """Tests manual rewrite detection thresholds and outputs."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    dec = AIDecision(
        decision_id="dec_rewrite_test",
        session_id="session_rewrite",
        timestamp=now - timedelta(minutes=2),
        provider="anthropic",
        model_id="claude-3-5-sonnet",
        task_type=TaskType.code_edit,
        suggestion_summary="Rewrite suggestions",
        suggestion_hash="hash_rewrite",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec)

    # 1. Rewrite below threshold (40% removed) -> None
    diff_low = {"removed": 40, "total_lines": 100}
    mistake_low = await detect_manual_rewrite(
        session_id="session_rewrite",
        file_path="app.py",
        diff_stats=diff_low,
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_low is None

    # 2. Rewrite above threshold (60% removed) -> Mistake
    diff_high = {"removed": 60, "total_lines": 100}
    mistake_high = await detect_manual_rewrite(
        session_id="session_rewrite",
        file_path="app.py",
        diff_stats=diff_high,
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_high is not None
    assert mistake_high.outcome_type == "correction"
    assert mistake_high.user_corrected is True
    assert mistake_high.correction_detail == "manual_rewrite"
    assert abs(mistake_high.feedback_signal - (-0.3)) < 1e-5
    assert mistake_high.severity == Severity.medium


@pytest.mark.asyncio
async def test_batch_ingestion(setup_test_db, tmp_path):
    """Tests batch ingestion of mistakes from a virtual log file."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    # Seed Decisions
    dec1 = AIDecision(
        decision_id="dec_log_1",
        session_id="session_log",
        timestamp=now - timedelta(minutes=8),
        provider="google",
        model_id="gemini",
        task_type=TaskType.code_edit,
        suggestion_summary="Log suggestion 1",
        suggestion_hash="hash_log_1",
        user_action=UserAction.accepted,
    )
    dec2 = AIDecision(
        decision_id="dec_log_2",
        session_id="session_log",
        timestamp=now - timedelta(minutes=4),
        provider="google",
        model_id="gemini",
        task_type=TaskType.command,
        suggestion_summary="Log suggestion 2",
        suggestion_hash="hash_log_2",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec1)
    await insert_decision(db_path, dec2)

    # Write log file
    log_file = tmp_path / "vexon_activity.log"
    events = [
        # 1. Valid file revert event
        {
            "event_type": "file_revert",
            "session_id": "session_log",
            "file_path": "index.js",
            "revert_context": {"lines_reverted": 15},
            "timestamp": now.isoformat(),
        },
        # 2. Valid build error event
        {
            "event_type": "build_error",
            "session_id": "session_log",
            "task_type": TaskType.command.value,
            "error_log": "compile error in main.go",
            "timestamp": now.isoformat(),
        },
        # 3. Invalid event (no decision resolves - session_invalid)
        {
            "event_type": "heavy_edit",
            "session_id": "session_invalid",
            "file_path": "app.py",
            "diff_stats": {"edit_ratio": 0.8},
            "timestamp": now.isoformat(),
        },
        # 4. Duplicate event (re-triggering same revert within 30s)
        {
            "event_type": "file_revert",
            "session_id": "session_log",
            "file_path": "index.js",
            "revert_context": {"lines_reverted": 15},
            "timestamp": (now + timedelta(seconds=10)).isoformat(),
        },
    ]

    with open(log_file, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    # Ingest logs
    count = await ingest_signals_from_vexon_logs(log_path=str(log_file), db_path=db_path)
    # Valid revert + valid build error = 2. Duplicate is deduplicated. Invalid session returns None.
    # Total new mistakes is 2.
    assert count == 2

    # Check mistakes list
    mistakes = await list_recent_mistakes(db_path, limit=10)
    assert len(mistakes) == 2


@pytest.mark.asyncio
async def test_per_task_type_thresholds(setup_test_db):
    """Verifies that the same edit ratio yields different outcomes for different task types near thresholds."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    # Seed decision for 'code'
    dec_code = AIDecision(
        decision_id="dec_threshold_code",
        session_id="session_thresh_code",
        timestamp=now - timedelta(minutes=4),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        suggestion_summary="code suggestion",
        suggestion_hash="hash_c",
        user_action=UserAction.accepted,
    )
    # Seed decision for 'chat'
    dec_chat = AIDecision(
        decision_id="dec_threshold_chat",
        session_id="session_thresh_chat",
        timestamp=now - timedelta(minutes=4),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.chat,
        suggestion_summary="chat suggestion",
        suggestion_hash="hash_ch",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec_code)
    await insert_decision(db_path, dec_chat)

    # Edit ratio is 0.40 (above 'code' threshold 0.30, but below 'chat' threshold 0.50)
    diff = {"edit_ratio": 0.40}

    # For code: should trigger a mistake (threshold is 0.30)
    mistake_code = await detect_heavy_edit(
        session_id="session_thresh_code",
        file_path="main.py",
        diff_stats=diff,
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_code is not None
    assert mistake_code.decision_id == "dec_threshold_code"

    # For chat: should NOT trigger a mistake (threshold is 0.50)
    mistake_chat = await detect_heavy_edit(
        session_id="session_thresh_chat",
        file_path="chat_output.txt",
        diff_stats=diff,
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_chat is None


@pytest.mark.asyncio
async def test_severity_bands(setup_test_db):
    """Verifies that events map correctly to different severity bands and feedback signals."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    dec = AIDecision(
        decision_id="dec_severity_test",
        session_id="session_sev",
        timestamp=now - timedelta(minutes=2),
        provider="anthropic",
        model_id="claude-3",
        task_type=TaskType.code_edit,
        suggestion_summary="code suggestion",
        suggestion_hash="hash_sev",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec)

    # Revert size < 5 -> Severity.low (base_severity -0.3, scaled by lines/50)
    mistake_low = await detect_file_revert(
        session_id="session_sev",
        file_path="foo.py",
        revert_context={"lines_reverted": 3},
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_low is not None
    assert mistake_low.severity == Severity.low
    # lines_reverted=3 -> magnitude_factor = 3/50 = 0.06 -> min(1.0, max(0.1, 0.06)) = 0.1 -> -0.3 * 0.1 = -0.03
    assert abs(mistake_low.feedback_signal - (-0.03)) < 1e-5

    # Revert size >= 20 -> Severity.high (base_severity -0.7, scaled by lines/50)
    # lines_reverted = 30 -> magnitude_factor = 30/50 = 0.6 -> -0.7 * 0.6 = -0.42
    mistake_high = await detect_file_revert(
        session_id="session_sev",
        file_path="foo.py",
        revert_context={"lines_reverted": 30},
        timestamp=now + timedelta(seconds=45),  # offset to bypass duplicate checks
        db_path=db_path,
    )
    assert mistake_high is not None
    assert mistake_high.severity == Severity.high
    assert abs(mistake_high.feedback_signal - (-0.42)) < 1e-5


@pytest.mark.asyncio
async def test_detect_immediate_retry(setup_test_db):
    """Tests the immediate retry detector with various time deltas."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    # Seed original decision
    dec = AIDecision(
        decision_id="dec_original",
        session_id="session_retry",
        timestamp=now - timedelta(seconds=10),
        provider="anthropic",
        model_id="claude-3",
        task_type=TaskType.code,
        suggestion_summary="code suggestion",
        suggestion_hash="hash_orig",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec)

    # 1. Immediate retry with short delta (15 seconds, max is 120) -> High Severity
    mistake_short = await detect_immediate_retry(
        session_id="session_retry",
        task_type="code",
        new_decision_id="dec_retry_1",
        time_delta_seconds=15.0,
        original_decision_id="dec_original",
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_short is not None
    assert mistake_short.outcome_type == "abandoned"
    assert mistake_short.user_corrected is False
    assert mistake_short.severity == Severity.high
    assert mistake_short.feedback_signal == -0.7

    # 2. Immediate retry with medium delta (60 seconds, max 120) -> Medium Severity
    # Use a different session to bypass duplicate checker
    dec_med = AIDecision(
        decision_id="dec_orig_med",
        session_id="session_retry_med",
        timestamp=now - timedelta(seconds=60),
        provider="anthropic",
        model_id="claude-3",
        task_type=TaskType.code,
        suggestion_summary="code suggestion",
        suggestion_hash="hash_med",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec_med)

    mistake_med = await detect_immediate_retry(
        session_id="session_retry_med",
        task_type="code",
        new_decision_id="dec_retry_2",
        time_delta_seconds=60.0,
        original_decision_id="dec_orig_med",
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_med is not None
    assert mistake_med.severity == Severity.medium
    assert mistake_med.feedback_signal == -0.5

    # 3. Delta > threshold (150s > 120s max for code) -> Should return None
    mistake_long = await detect_immediate_retry(
        session_id="session_retry_med",
        task_type="code",
        new_decision_id="dec_retry_3",
        time_delta_seconds=150.0,
        original_decision_id="dec_orig_med",
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_long is None


@pytest.mark.asyncio
async def test_detect_test_fix_loop(setup_test_db):
    """Tests the test-fix loop detector under success and cycle-scaling conditions."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    # Seed decision
    dec = AIDecision(
        decision_id="dec_loop_orig",
        session_id="session_loop",
        timestamp=now - timedelta(minutes=5),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        suggestion_summary="Original loop code",
        suggestion_hash="hash_loop",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec)

    # 1. Serious test failure + heavy edit -> Test-Fix Loop Mistake (High Severity)
    edit_events = [
        {"timestamp": (now - timedelta(minutes=2)).isoformat(), "diff_stats": {"edit_ratio": 0.40}}
    ]
    mistake_loop = await detect_test_fix_loop(
        session_id="session_loop",
        file_path="main.py",
        original_decision_id="dec_loop_orig",
        test_failure_log="AssertionError: test_code failed",
        edit_events=edit_events,
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_loop is not None
    assert mistake_loop.outcome_type == "correction"
    assert mistake_loop.user_corrected is True
    assert mistake_loop.severity == Severity.high
    # 1 cycle -> feedback_signal = -0.7
    assert mistake_loop.feedback_signal == -0.7

    # 2. Upgrade to higher penalty on multiple cycles
    # Seed new decision / session
    dec_multi = AIDecision(
        decision_id="dec_loop_multi",
        session_id="session_loop_multi",
        timestamp=now - timedelta(minutes=5),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        suggestion_summary="Original multi loop code",
        suggestion_hash="hash_multi",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec_multi)

    # 3 edit events (cycles)
    multi_edits = [
        {"timestamp": (now - timedelta(minutes=4)).isoformat(), "diff_stats": {"edit_ratio": 0.15}},
        {"timestamp": (now - timedelta(minutes=3)).isoformat(), "diff_stats": {"edit_ratio": 0.20}},
        {"timestamp": (now - timedelta(minutes=2)).isoformat(), "diff_stats": {"edit_ratio": 0.45}}, # heavy edit
    ]
    mistake_multi = await detect_test_fix_loop(
        session_id="session_loop_multi",
        file_path="main.py",
        original_decision_id="dec_loop_multi",
        test_failure_log="AssertionError: test failed",
        edit_events=multi_edits,
        timestamp=now,
        db_path=db_path,
    )
    assert mistake_multi is not None
    # 3 cycles -> feedback_signal = -0.7 - min(0.3, 2 * 0.1) = -0.9
    assert abs(mistake_multi.feedback_signal - (-0.9)) < 1e-5


@pytest.mark.asyncio
async def test_batch_ingestion_new_signals(setup_test_db, tmp_path):
    """Tests log ingestion for immediate_retry and test_fix_loop signals."""
    db_path = setup_test_db
    now = datetime.now(timezone.utc)

    # Seed Decisions
    dec1 = AIDecision(
        decision_id="dec_retry_log",
        session_id="session_batch_new",
        timestamp=now - timedelta(seconds=20),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        suggestion_summary="Original command",
        suggestion_hash="hash_orig_log",
        user_action=UserAction.accepted,
    )
    dec2 = AIDecision(
        decision_id="dec_loop_log",
        session_id="session_batch_new",
        timestamp=now - timedelta(minutes=5),
        provider="openai",
        model_id="gpt-4o",
        task_type=TaskType.code,
        suggestion_summary="Original loop command",
        suggestion_hash="hash_loop_log",
        user_action=UserAction.accepted,
    )
    await insert_decision(db_path, dec1)
    await insert_decision(db_path, dec2)

    log_file = tmp_path / "vexon_new_signals.log"
    events = [
        # 1. Immediate retry event
        {
            "event_type": "immediate_retry",
            "session_id": "session_batch_new",
            "task_type": "code",
            "new_decision_id": "dec_retry_log_retry",
            "time_delta_seconds": 20.0,
            "original_decision_id": "dec_retry_log",
            "timestamp": now.isoformat(),
        },
        # 2. Test fix loop event
        {
            "event_type": "test_fix_loop",
            "session_id": "session_batch_new",
            "file_path": "server.js",
            "original_decision_id": "dec_loop_log",
            "test_failure_log": "AssertionError: server failed",
            "edit_events": [
                {"timestamp": (now - timedelta(minutes=2)).isoformat(), "diff_stats": {"edit_ratio": 0.40}}
            ],
            "timestamp": now.isoformat(),
        }
    ]

    with open(log_file, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")

    count = await ingest_signals_from_vexon_logs(log_path=str(log_file), db_path=db_path)
    assert count == 2

    mistakes = await list_recent_mistakes(db_path, limit=10)
    assert len(mistakes) == 2

