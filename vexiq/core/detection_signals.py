"""Auto-Detection Signals evaluator.

Interprets system indicators (git file reverts, build error streams, test execution status,
heavy editing patterns, immediate retries, test-fix loops) to automatically verify and create AI mistake records.
"""

import os
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta

from vexiq.config import get_settings
from vexiq.models import AIMistake, TaskType, FailureType, Severity, DecisionOutcome
from vexiq.db import get_decision_by_id, update_decision_outcome, get_db_conn
from vexiq.core.mistake_tracker import MistakeTracker

# Logger setup
logger = logging.getLogger(__name__)

# Configurable Thresholds per task type
TASK_TYPE_THRESHOLDS = {
    "code": {
        "heavy_edit_threshold": 0.30,
        "manual_rewrite_threshold": 0.50,
        "time_window_minutes": 10,
        "immediate_retry_max_seconds": 120,
        "test_fix_max_minutes": 15,
    },
    "chat": {
        "heavy_edit_threshold": 0.50,   # chat tolerates more editing
        "manual_rewrite_threshold": 0.70,
        "time_window_minutes": 5,
        "immediate_retry_max_seconds": 60,
        "test_fix_max_minutes": None,   # not applicable
    },
    "command": {
        "heavy_edit_threshold": 0.25,
        "manual_rewrite_threshold": 0.40,
        "time_window_minutes": 15,
        "immediate_retry_max_seconds": 180,
        "test_fix_max_minutes": 20,
    },
    "architecture": {
        "heavy_edit_threshold": 0.40,
        "manual_rewrite_threshold": 0.60,
        "time_window_minutes": 30,
        "immediate_retry_max_seconds": 300,
        "test_fix_max_minutes": None,
    },
    "artifact": {
        "heavy_edit_threshold": 0.35,
        "manual_rewrite_threshold": 0.55,
        "time_window_minutes": 15,
        "immediate_retry_max_seconds": 120,
        "test_fix_max_minutes": None,
    },
}

# Severity Band Base Values
SEVERITY_BANDS = {
    Severity.low: -0.3,
    Severity.medium: -0.5,
    Severity.high: -0.7,
    Severity.critical: -0.9,
}

# Build error severity patterns mapped to feedback signal values (negative float penalty)
BUILD_ERROR_SEVERITY_LEVELS = {
    "syntaxerror": -0.8,
    "compile error": -0.9,
    "segmentation fault": -1.0,
    "failed test": -0.7,
    "assertionerror": -0.6,
    "exception": -0.5,
}

# Default vexon activity log path
DEFAULT_LOG_PATH = os.path.expanduser("~/.vexiq/vexon_activity.log")

TIME_WINDOW_MINUTES = 10


def _get_thresholds(task_type: str | TaskType) -> dict:
    """Gets threshold configuration for a given task type, using 'code' as the fallback."""
    tt_str = task_type.value if isinstance(task_type, TaskType) else str(task_type)
    if tt_str == "code_edit":
        tt_key = "code"
    elif tt_str in TASK_TYPE_THRESHOLDS:
        tt_key = tt_str
    else:
        tt_key = "code"  # Safe default fallback
    return TASK_TYPE_THRESHOLDS[tt_key]


async def resolve_decision_id(
    session_id: str,
    task_type: str,
    timestamp: datetime,
    window_minutes: int = 10,
    db_path: str | None = None,
) -> str | None:
    """Resolves the decision_id of the closest decision matching session_id, task_type, and timestamp.

    Finds the closest decision that occurred BEFORE the event timestamp, within the window_minutes limit.
    """
    db_path = db_path or get_settings().vexiq_db_path
    
    # Standardize event timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
        
    event_timestamp_str = timestamp.isoformat()

    # Query all preceding decisions matching session and task type
    query = """
        SELECT decision_id, timestamp FROM ai_decisions
        WHERE session_id = ? AND task_type = ? AND timestamp <= ?
        ORDER BY timestamp DESC
    """
    
    async with get_db_conn(db_path) as db:
        async with db.execute(query, (session_id, task_type, event_timestamp_str)) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                dec_id = row["decision_id"]
                dec_time = datetime.fromisoformat(row["timestamp"])
                
                if dec_time.tzinfo is None:
                    dec_time = dec_time.replace(tzinfo=timezone.utc)
                else:
                    dec_time = dec_time.astimezone(timezone.utc)

                # Since they are ordered by timestamp DESC, the first one is the closest preceding decision
                diff = timestamp - dec_time
                if diff <= timedelta(minutes=window_minutes):
                    return dec_id
                else:
                    # Since ordered DESC, subsequent decisions will be even older and outside the window
                    break
                    
    return None


async def detect_file_revert(
    session_id: str,
    file_path: str,
    revert_context: dict | None = None,
    timestamp: datetime | None = None,
    db_path: str | None = None,
) -> AIMistake | None:
    """Detects a mistake from a file revert event and logs it.

    Triggered when a user/system reverts a file to a previous version.
    """
    db_path = db_path or get_settings().vexiq_db_path
    event_time = timestamp or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    # Reverts affect code, search first under code_edit, fallback to code task types
    decision_id = await resolve_decision_id(
        session_id, TaskType.code_edit.value, event_time, window_minutes=60, db_path=db_path
    )
    if not decision_id:
        decision_id = await resolve_decision_id(
            session_id, TaskType.code.value, event_time, window_minutes=60, db_path=db_path
        )

    if not decision_id:
        logger.warning(
            f"Could not resolve AI decision for revert event in session {session_id} on file {file_path}"
        )
        return None

    decision = await get_decision_by_id(db_path, decision_id)
    if not decision:
        return None

    revert_context = revert_context or {}
    lines_reverted = revert_context.get("lines_reverted", 10)

    # Determine Severity based on size of revert
    if lines_reverted >= 20:
        severity = Severity.high
    elif lines_reverted >= 5:
        severity = Severity.medium
    else:
        severity = Severity.low

    # Scale feedback signal by severity and magnitude
    base_severity = SEVERITY_BANDS[severity]
    magnitude_factor = max(0.1, lines_reverted / 50.0)
    feedback_signal = base_severity * min(1.0, magnitude_factor)

    # Create AIMistake record
    mistake_id = str(uuid.uuid4())
    mistake = AIMistake(
        mistake_id=mistake_id,
        decision_id=decision_id,
        session_id=session_id,
        project_id=decision.project_id,
        task_id=decision.task_id,
        timestamp=event_time,
        provider=decision.provider,
        model_id=decision.model_id,
        task_type=decision.task_type,
        failure_type=FailureType.wrong_code,
        failure_summary=f"File {file_path} reverted ({lines_reverted} lines changed).",
        correction_made=True,
        correction_summary="file_revert",
        severity=severity,
        auto_detected=True,
        detection_signal="file_revert",
        # Transients for tracking/testing
        outcome_type="revert",
        user_corrected=True,
        correction_detail="file_revert",
        feedback_signal=feedback_signal,
    )

    tracker = MistakeTracker(db_path)
    result = await tracker.create_mistake(mistake)

    # Update decision outcome to reverted
    await update_decision_outcome(db_path, decision_id, DecisionOutcome.reverted, event_time)

    # Check if a duplicate was returned
    is_dup = (result.mistake_id != mistake_id)
    result.is_duplicate = is_dup
    
    # Force properties on the returned object (even if duplicate) for caller validation
    result.outcome_type = "revert"
    result.user_corrected = True
    result.correction_detail = "file_revert"
    result.feedback_signal = feedback_signal

    return result


async def detect_heavy_edit(
    session_id: str,
    file_path: str,
    diff_stats: dict,
    timestamp: datetime | None = None,
    db_path: str | None = None,
) -> AIMistake | None:
    """Detects a mistake from a heavy edit pattern and logs it.

    Triggered when a user heavily edits AI output shortly after generation.
    """
    db_path = db_path or get_settings().vexiq_db_path
    event_time = timestamp or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    # Try resolving matching decision first to determine the task type and config thresholds
    decision_id = None
    task_type_str = "code"
    
    possible_types = [
        TaskType.code_edit.value,
        TaskType.code.value,
        TaskType.chat.value,
        TaskType.command.value,
        TaskType.artifact.value,
        TaskType.architecture.value,
        TaskType.config.value,
        TaskType.other.value
    ]
    
    for tt in possible_types:
        dec_id = await resolve_decision_id(
            session_id, tt, event_time, window_minutes=TIME_WINDOW_MINUTES, db_path=db_path
        )
        if dec_id:
            decision_id = dec_id
            task_type_str = tt
            break

    config = _get_thresholds(task_type_str)

    # Check time window if present in diff_stats
    time_delta_seconds = diff_stats.get("time_delta_seconds")
    max_window_minutes = config.get("time_window_minutes", TIME_WINDOW_MINUTES)
    if time_delta_seconds is not None and time_delta_seconds > max_window_minutes * 60:
        return None

    # Calculate edit ratio
    edit_ratio = diff_stats.get("edit_ratio")
    if edit_ratio is None:
        added = diff_stats.get("added", 0)
        removed = diff_stats.get("removed", 0)
        changed = diff_stats.get("changed", 0)
        total_lines = diff_stats.get("total_lines", 100)
        edit_ratio = (added + removed + changed) / total_lines if total_lines > 0 else 0.0

    threshold = config["heavy_edit_threshold"]
    if edit_ratio <= threshold:
        return None

    if not decision_id:
        logger.warning(
            f"Could not resolve AI decision for heavy edit event in session {session_id} on file {file_path}"
        )
        return None

    decision = await get_decision_by_id(db_path, decision_id)
    if not decision:
        return None

    # Severity scale based on edit ratio
    if edit_ratio >= 0.6:
        severity = Severity.high
    elif edit_ratio >= 0.4:
        severity = Severity.medium
    else:
        severity = Severity.low

    base_severity = SEVERITY_BANDS[severity]
    feedback_signal = base_severity * min(1.0, edit_ratio)

    mistake_id = str(uuid.uuid4())
    mistake = AIMistake(
        mistake_id=mistake_id,
        decision_id=decision_id,
        session_id=session_id,
        project_id=decision.project_id,
        task_id=decision.task_id,
        timestamp=event_time,
        provider=decision.provider,
        model_id=decision.model_id,
        task_type=decision.task_type,
        failure_type=FailureType.wrong_code,
        failure_summary=f"Heavy edit detected on {file_path} (edit ratio: {edit_ratio:.2%}).",
        correction_made=True,
        correction_summary="heavy_edit",
        severity=severity,
        auto_detected=True,
        detection_signal="heavy_edit",
        # Transients
        outcome_type="correction",
        user_corrected=True,
        correction_detail="heavy_edit",
        feedback_signal=feedback_signal,
    )

    tracker = MistakeTracker(db_path)
    result = await tracker.create_mistake(mistake)

    # Update decision outcome to edited_further
    await update_decision_outcome(db_path, decision_id, DecisionOutcome.edited_further, event_time)

    # Check duplicate
    is_dup = (result.mistake_id != mistake_id)
    result.is_duplicate = is_dup

    # Force properties
    result.outcome_type = "correction"
    result.user_corrected = True
    result.correction_detail = "heavy_edit"
    result.feedback_signal = feedback_signal

    return result


async def detect_build_error(
    session_id: str,
    task_type: str,
    error_log: str,
    timestamp: datetime | None = None,
    db_path: str | None = None,
) -> AIMistake | None:
    """Detects a mistake from a compile/test run error and logs it.

    Triggered when a command or build fails immediately after AI code application.
    """
    db_path = db_path or get_settings().vexiq_db_path
    event_time = timestamp or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    # Determine feedback_signal value based on error pattern matches
    feedback_signal = -0.4  # Default build failure penalty
    error_log_lower = error_log.lower()
    for pattern, penalty in BUILD_ERROR_SEVERITY_LEVELS.items():
        if pattern in error_log_lower:
            feedback_signal = min(feedback_signal, penalty)  # Capture most severe failure

    # Resolve decision
    decision_id = await resolve_decision_id(
        session_id, task_type, event_time, window_minutes=5, db_path=db_path
    )
    if not decision_id:
        logger.warning(
            f"Could not resolve AI decision for build error in session {session_id} for task type {task_type}"
        )
        return None

    decision = await get_decision_by_id(db_path, decision_id)
    if not decision:
        return None

    # Severity mapping based on feedback signal value
    if feedback_signal <= -0.9:
        severity = Severity.critical
    elif feedback_signal <= -0.7:
        severity = Severity.high
    elif feedback_signal <= -0.45:
        severity = Severity.medium
    else:
        severity = Severity.low
    
    # Choose failure type based on error log content
    failure_type = FailureType.broken_build
    if "failed test" in error_log_lower or "test_failure" in error_log_lower or "assertionerror" in error_log_lower:
        failure_type = FailureType.test_failure

    mistake_id = str(uuid.uuid4())
    mistake = AIMistake(
        mistake_id=mistake_id,
        decision_id=decision_id,
        session_id=session_id,
        project_id=decision.project_id,
        task_id=decision.task_id,
        timestamp=event_time,
        provider=decision.provider,
        model_id=decision.model_id,
        task_type=decision.task_type,
        failure_type=failure_type,
        failure_summary=f"Build/Command failed: {error_log[:150]}",
        correction_made=False,
        correction_summary=None,
        severity=severity,
        auto_detected=True,
        detection_signal="build_failure",
        # Transients
        outcome_type="failure",
        user_corrected=False,
        correction_detail="build_error",
        feedback_signal=feedback_signal,
    )

    tracker = MistakeTracker(db_path)
    result = await tracker.create_mistake(mistake)

    is_dup = (result.mistake_id != mistake_id)
    result.is_duplicate = is_dup

    # Force properties
    result.outcome_type = "failure"
    result.user_corrected = False
    result.correction_detail = "build_error"
    result.feedback_signal = feedback_signal

    return result


async def detect_manual_rewrite(
    session_id: str,
    file_path: str,
    diff_stats: dict,
    timestamp: datetime | None = None,
    db_path: str | None = None,
) -> AIMistake | None:
    """Detects a mistake from a manual rewrite event and logs it.

    Triggered when a user manually overwrites a large percentage of AI suggestions.
    """
    db_path = db_path or get_settings().vexiq_db_path
    event_time = timestamp or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    # Try resolving matching decision first to determine config thresholds
    decision_id = None
    task_type_str = "code"
    
    possible_types = [
        TaskType.code_edit.value,
        TaskType.code.value,
        TaskType.chat.value,
        TaskType.command.value,
        TaskType.artifact.value,
        TaskType.architecture.value,
        TaskType.config.value,
        TaskType.other.value
    ]
    
    for tt in possible_types:
        dec_id = await resolve_decision_id(
            session_id, tt, event_time, window_minutes=TIME_WINDOW_MINUTES, db_path=db_path
        )
        if dec_id:
            decision_id = dec_id
            task_type_str = tt
            break

    config = _get_thresholds(task_type_str)

    # Check rewrite ratio
    rewrite_ratio = diff_stats.get("rewrite_ratio")
    if rewrite_ratio is None:
        removed = diff_stats.get("removed", 0)
        total_lines = diff_stats.get("total_lines", 100)
        rewrite_ratio = removed / total_lines if total_lines > 0 else 0.0

    threshold = config["manual_rewrite_threshold"]
    if rewrite_ratio <= threshold:
        return None

    if not decision_id:
        logger.warning(
            f"Could not resolve AI decision for manual rewrite in session {session_id} on file {file_path}"
        )
        return None

    decision = await get_decision_by_id(db_path, decision_id)
    if not decision:
        return None

    # Severity scale based on rewrite ratio
    if rewrite_ratio >= 0.8:
        severity = Severity.high
    elif rewrite_ratio >= 0.6:
        severity = Severity.medium
    else:
        severity = Severity.low

    base_severity = SEVERITY_BANDS[severity]
    feedback_signal = base_severity * min(1.0, rewrite_ratio)

    mistake_id = str(uuid.uuid4())
    mistake = AIMistake(
        mistake_id=mistake_id,
        decision_id=decision_id,
        session_id=session_id,
        project_id=decision.project_id,
        task_id=decision.task_id,
        timestamp=event_time,
        provider=decision.provider,
        model_id=decision.model_id,
        task_type=decision.task_type,
        failure_type=FailureType.wrong_code,
        failure_summary=f"Manual rewrite detected on {file_path} (rewrite ratio: {rewrite_ratio:.2%}).",
        correction_made=True,
        correction_summary="manual_rewrite",
        severity=severity,
        auto_detected=True,
        detection_signal="manual_rewrite",
        # Transients
        outcome_type="correction",
        user_corrected=True,
        correction_detail="manual_rewrite",
        feedback_signal=feedback_signal,
    )

    tracker = MistakeTracker(db_path)
    result = await tracker.create_mistake(mistake)

    # Update decision outcome to edited_further
    await update_decision_outcome(db_path, decision_id, DecisionOutcome.edited_further, event_time)

    is_dup = (result.mistake_id != mistake_id)
    result.is_duplicate = is_dup

    # Force properties
    result.outcome_type = "correction"
    result.user_corrected = True
    result.correction_detail = "manual_rewrite"
    result.feedback_signal = feedback_signal

    return result


async def detect_immediate_retry(
    session_id: str,
    task_type: str,
    new_decision_id: str,
    time_delta_seconds: float,
    original_decision_id: str | None = None,
    prompt_similarity: float | None = None,
    timestamp: datetime | None = None,
    db_path: str | None = None,
) -> AIMistake | None:
    """Detects a mistake from an immediate retry event and logs it.

    Triggered when a user immediately re-issues the same task shortly after an AI response.
    """
    db_path = db_path or get_settings().vexiq_db_path
    event_time = timestamp or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    # Fetch task thresholds
    config = _get_thresholds(task_type)
    max_seconds = config.get("immediate_retry_max_seconds", 120)

    if time_delta_seconds > max_seconds:
        return None

    # Resolve original decision_id
    resolved_id = original_decision_id
    if not resolved_id:
        # Query all decisions preceding event_time, filtering out the new retry decision if it's already logged
        query = """
            SELECT decision_id FROM ai_decisions
            WHERE session_id = ? AND task_type = ? AND timestamp <= ?
            ORDER BY timestamp DESC
        """
        async with get_db_conn(db_path) as db:
            async with db.execute(query, (session_id, task_type, event_time.isoformat())) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    dec_id = row["decision_id"]
                    if dec_id != new_decision_id:
                        resolved_id = dec_id
                        break

    if not resolved_id:
        logger.warning(
            f"Could not resolve original AI decision for immediate retry in session {session_id}"
        )
        return None

    decision = await get_decision_by_id(db_path, resolved_id)
    if not decision:
        return None

    # Scale severity based on time_delta_seconds
    if time_delta_seconds < max_seconds * 0.25:
        severity = Severity.high
    elif time_delta_seconds <= max_seconds * 0.75:
        severity = Severity.medium
    else:
        severity = Severity.low

    feedback_signal = SEVERITY_BANDS[severity]

    mistake_id = str(uuid.uuid4())
    mistake = AIMistake(
        mistake_id=mistake_id,
        decision_id=resolved_id,
        session_id=session_id,
        project_id=decision.project_id,
        task_id=decision.task_id,
        timestamp=event_time,
        provider=decision.provider,
        model_id=decision.model_id,
        task_type=decision.task_type,
        failure_type=FailureType.explicit_rejection,
        failure_summary=f"Immediate retry of task after {time_delta_seconds}s.",
        correction_made=False,
        correction_summary=None,
        severity=severity,
        auto_detected=True,
        detection_signal="immediate_retry",
        # Transients
        outcome_type="abandoned",
        user_corrected=False,
        correction_detail="immediate_retry",
        feedback_signal=feedback_signal,
    )

    tracker = MistakeTracker(db_path)
    result = await tracker.create_mistake(mistake)

    # Update original decision outcome to reverted
    await update_decision_outcome(db_path, resolved_id, DecisionOutcome.reverted, event_time)

    # Check duplicate
    is_dup = (result.mistake_id != mistake_id)
    result.is_duplicate = is_dup

    result.outcome_type = "abandoned"
    result.user_corrected = False
    result.correction_detail = "immediate_retry"
    result.feedback_signal = feedback_signal

    return result


async def detect_test_fix_loop(
    session_id: str,
    file_path: str,
    original_decision_id: str | None = None,
    test_failure_log: str | None = None,
    edit_events: list[dict] | None = None,
    test_success_event: dict | None = None,
    timestamp: datetime | None = None,
    db_path: str | None = None,
) -> AIMistake | None:
    """Detects a mistake from a test fail -> edit -> fix code loop.

    Triggered when test failures are immediately followed by edits and/or subsequent pass.
    """
    db_path = db_path or get_settings().vexiq_db_path
    event_time = timestamp or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    # Code tasks only
    config = _get_thresholds("code")
    max_minutes = config.get("test_fix_max_minutes")
    if max_minutes is None:
        return None

    # Resolve original decision_id
    resolved_id = original_decision_id
    if not resolved_id:
        resolved_id = await resolve_decision_id(
            session_id, TaskType.code_edit.value, event_time, window_minutes=max_minutes, db_path=db_path
        )
        if not resolved_id:
            resolved_id = await resolve_decision_id(
                session_id, TaskType.code.value, event_time, window_minutes=max_minutes, db_path=db_path
            )

    if not resolved_id:
        logger.warning(
            f"Could not resolve AI decision for test-fix loop in session {session_id} on file {file_path}"
        )
        return None

    decision = await get_decision_by_id(db_path, resolved_id)
    if not decision:
        return None

    # Verify if edit events show heavy edits or if a heavy edit was logged in the DB
    edit_events = edit_events or []
    has_heavy_edit = False
    
    # 1. Check passed edit events
    for ev in edit_events:
        diff_stats = ev.get("diff_stats", {})
        ratio = diff_stats.get("edit_ratio")
        if ratio is None:
            added = diff_stats.get("added", 0)
            removed = diff_stats.get("removed", 0)
            changed = diff_stats.get("changed", 0)
            total = diff_stats.get("total_lines", 100)
            ratio = (added + removed + changed) / total if total > 0 else 0.0
        if ratio >= config["heavy_edit_threshold"]:
            has_heavy_edit = True
            break
            
    # 2. Check if a heavy edit is already logged in DB for this decision (for historical trace integration)
    if not has_heavy_edit:
        query = """
            SELECT 1 FROM ai_mistakes
            WHERE decision_id = ? AND detection_signal = 'heavy_edit'
        """
        async with get_db_conn(db_path) as db:
            async with db.execute(query, (resolved_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    has_heavy_edit = True

    # If no heavy edit occurred, this isn't a test-fix loop (it might just be a build error on its own)
    if not has_heavy_edit:
        return None

    # Determine base severity from the test failure log
    test_failure_log = test_failure_log or ""
    log_lower = test_failure_log.lower()
    
    if not log_lower:
        base_severity = -0.5
        severity = Severity.medium
    elif "assertionerror" in log_lower or "compile error" in log_lower or "crash" in log_lower or "segmentation fault" in log_lower:
        base_severity = -0.7
        severity = Severity.high
    else:
        base_severity = -0.5
        severity = Severity.medium

    cycles = len(edit_events) if edit_events else 1
    # Upgrade to high if multiple edit attempts
    if cycles >= 3:
        base_severity = -0.7
        severity = Severity.high

    # Scale feedback_signal based on severity and edit cycles (more cycles -> stronger negative)
    feedback_signal = base_severity - min(0.3, (cycles - 1) * 0.1)

    mistake_id = str(uuid.uuid4())
    mistake = AIMistake(
        mistake_id=mistake_id,
        decision_id=resolved_id,
        session_id=session_id,
        project_id=decision.project_id,
        task_id=decision.task_id,
        timestamp=event_time,
        provider=decision.provider,
        model_id=decision.model_id,
        task_type=decision.task_type,
        failure_type=FailureType.test_failure,
        failure_summary=f"Test-Fix loop detected for {file_path} with {cycles} edit cycles.",
        correction_made=True,
        correction_summary="test_fix_loop",
        severity=severity,
        auto_detected=True,
        detection_signal="test_fix_loop",
        # Transients
        outcome_type="correction",
        user_corrected=True,
        correction_detail="test_fix_loop",
        feedback_signal=feedback_signal,
    )

    tracker = MistakeTracker(db_path)
    result = await tracker.create_mistake(mistake)

    # Update decision outcome to edited_further
    await update_decision_outcome(db_path, resolved_id, DecisionOutcome.edited_further, event_time)

    # Check duplicate
    is_dup = (result.mistake_id != mistake_id)
    result.is_duplicate = is_dup

    result.outcome_type = "correction"
    result.user_corrected = True
    result.correction_detail = "test_fix_loop"
    result.feedback_signal = feedback_signal

    return result


async def ingest_signals_from_vexon_logs(
    log_path: str | None = None,
    db_path: str | None = None,
) -> int:
    """Batch processes a Vexon log file of system events.

    Parses events line-by-line, runs the appropriate detector, and logs mistakes.
    Returns the count of newly recorded mistakes.
    """
    db_path = db_path or get_settings().vexiq_db_path
    log_path = log_path or DEFAULT_LOG_PATH

    if not os.path.exists(log_path):
        logger.warning(f"Vexon activity log file not found at: {log_path}")
        return 0

    new_mistakes_count = 0
    
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    event_type = event.get("event_type")
                    session_id = event.get("session_id")
                    
                    if not event_type or not session_id:
                        continue

                    # Parse timestamp
                    ts_str = event.get("timestamp")
                    ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(timezone.utc)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)

                    mistake = None

                    if event_type == "file_revert":
                        mistake = await detect_file_revert(
                            session_id=session_id,
                            file_path=event.get("file_path", ""),
                            revert_context=event.get("revert_context"),
                            timestamp=ts,
                            db_path=db_path,
                        )
                    elif event_type == "heavy_edit":
                        mistake = await detect_heavy_edit(
                            session_id=session_id,
                            file_path=event.get("file_path", ""),
                            diff_stats=event.get("diff_stats", {}),
                            timestamp=ts,
                            db_path=db_path,
                        )
                    elif event_type == "build_error":
                        mistake = await detect_build_error(
                            session_id=session_id,
                            task_type=event.get("task_type", ""),
                            error_log=event.get("error_log", ""),
                            timestamp=ts,
                            db_path=db_path,
                        )
                    elif event_type == "manual_rewrite":
                        mistake = await detect_manual_rewrite(
                            session_id=session_id,
                            file_path=event.get("file_path", ""),
                            diff_stats=event.get("diff_stats", {}),
                            timestamp=ts,
                            db_path=db_path,
                        )
                    elif event_type == "immediate_retry":
                        mistake = await detect_immediate_retry(
                            session_id=session_id,
                            task_type=event.get("task_type", ""),
                            new_decision_id=event.get("new_decision_id", ""),
                            time_delta_seconds=event.get("time_delta_seconds", 0.0),
                            original_decision_id=event.get("original_decision_id"),
                            prompt_similarity=event.get("prompt_similarity"),
                            timestamp=ts,
                            db_path=db_path,
                        )
                    elif event_type == "test_fix_loop":
                        mistake = await detect_test_fix_loop(
                            session_id=session_id,
                            file_path=event.get("file_path", ""),
                            original_decision_id=event.get("original_decision_id"),
                            test_failure_log=event.get("test_failure_log"),
                            edit_events=event.get("edit_events"),
                            test_success_event=event.get("test_success_event"),
                            timestamp=ts,
                            db_path=db_path,
                        )

                    # Only count if the mistake was successfully created and was NOT a duplicate
                    if mistake and not getattr(mistake, "is_duplicate", False):
                        new_mistakes_count += 1
                        
                except Exception as e:
                    logger.error(f"Error processing log event line: {e}")
                    continue
    except Exception as e:
        logger.error(f"Failed to read Vexon activity log file: {e}")
        return 0

    return new_mistakes_count
