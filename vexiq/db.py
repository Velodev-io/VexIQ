"""Database utility module for VexIQ.

Manages SQLite schema definitions, migrations, connection lifecycle, and asynchronous 
query executions utilizing aiosqlite.
"""

import os
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator
import aiosqlite

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


@asynccontextmanager
async def get_db_conn(db_path: str) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Provides a connection context manager yielding an aiosqlite connection with configured pragmas.

    Args:
        db_path: Absolute path to the SQLite database.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("PRAGMA synchronous = NORMAL;")
        yield db


async def init_db(db_path: str) -> None:
    """Initializes the SQLite database, creating directory parents, running performance pragmas,

    creating schemas, and setting indices.

    Args:
        db_path: Absolute path to the SQLite database.
    """
    # 1. Create parent directory if needed
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # 2. Connect and run initialization
    async with aiosqlite.connect(db_path) as db:
        # Run WAL/Synchronous Pragmas
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA synchronous = NORMAL;")
        await db.execute("PRAGMA foreign_keys = ON;")

        # Create ai_decisions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_decisions (
                decision_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                project_id TEXT,
                task_id TEXT,
                timestamp TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                suggestion_summary TEXT NOT NULL,
                suggestion_hash TEXT NOT NULL,
                user_action TEXT NOT NULL,
                modification_summary TEXT,
                outcome TEXT NOT NULL,
                outcome_recorded_at TEXT,
                confidence_score REAL,
                routing_metadata TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

        # Create ai_mistakes table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_mistakes (
                mistake_id TEXT PRIMARY KEY,
                decision_id TEXT,
                session_id TEXT NOT NULL,
                project_id TEXT,
                task_id TEXT,
                timestamp TEXT NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                failure_type TEXT NOT NULL,
                failure_summary TEXT NOT NULL,
                correction_made INTEGER NOT NULL,
                correction_summary TEXT,
                severity TEXT NOT NULL,
                auto_detected INTEGER NOT NULL,
                detection_signal TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

        # Check if provider_profiles exists and is outdated
        cursor = await db.execute("PRAGMA table_info(provider_profiles)")
        cols = [row[1] for row in await cursor.fetchall()]
        if cols and "successful_decisions" not in cols:
            await db.execute("DROP TABLE provider_profiles")

        # Create provider_profiles table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS provider_profiles (
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                task_type TEXT NOT NULL,
                total_decisions INTEGER NOT NULL,
                successful_decisions INTEGER NOT NULL,
                mistake_count INTEGER NOT NULL,
                user_reported_mistakes INTEGER NOT NULL,
                correction_count INTEGER NOT NULL,
                revert_count INTEGER NOT NULL,
                heavy_edit_count INTEGER NOT NULL,
                build_error_count INTEGER NOT NULL,
                avg_feedback_score REAL,
                avg_latency_ms REAL,
                success_rate REAL NOT NULL,
                acceptance_rate REAL NOT NULL,
                modification_rate REAL NOT NULL,
                rejection_rate REAL NOT NULL,
                revert_rate REAL NOT NULL,
                mistake_rate REAL NOT NULL,
                correction_rate REAL NOT NULL,
                heavy_edit_rate REAL NOT NULL,
                build_error_rate REAL NOT NULL,
                last_seen_at TEXT,
                profile_confidence TEXT NOT NULL,
                confidence_factor REAL NOT NULL,
                quality_score REAL NOT NULL,
                cold_start INTEGER NOT NULL,
                sample_size_bucket TEXT NOT NULL,
                mistake_by_type TEXT NOT NULL,
                avg_confidence REAL,
                routing_score REAL,
                last_updated TEXT NOT NULL,
                PRIMARY KEY (provider, model_id, task_type)
            );
        """)

        # Create routing_decisions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS routing_decisions (
                routing_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                selected_provider TEXT NOT NULL,
                selected_model TEXT NOT NULL,
                score REAL,
                competing_providers TEXT NOT NULL,
                cold_start INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            );
        """)

        # Create indices
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_decisions_provider_model_task 
            ON ai_decisions (provider, model_id, task_type);
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_decisions_session_timestamp 
            ON ai_decisions (session_id, timestamp);
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_decisions_project_task 
            ON ai_decisions (project_id, task_id);
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_mistakes_provider_model_task 
            ON ai_mistakes (provider, model_id, task_type);
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_mistakes_session_timestamp 
            ON ai_mistakes (session_id, timestamp);
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_routing_decisions_task_timestamp 
            ON routing_decisions (task_type, timestamp);
        """)

        await db.commit()


async def table_exists(db_path: str, table_name: str) -> bool:
    """Checks if a given table exists in the database.

    Args:
        db_path: Absolute path to the SQLite database.
        table_name: Name of the table to check.
    """
    try:
        async with get_db_conn(db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None
    except Exception:
        return False


async def get_table_counts(db_path: str) -> dict[str, int]:
    """Retrieves current row counts for all VexIQ tables.

    Args:
        db_path: Absolute path to the SQLite database.
    """
    counts = {
        "ai_decisions": 0,
        "ai_mistakes": 0,
        "provider_profiles": 0,
        "routing_decisions": 0,
    }
    for table in counts.keys():
        if await table_exists(db_path, table):
            try:
                async with get_db_conn(db_path) as db:
                    async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                        row = await cursor.fetchone()
                        if row:
                            counts[table] = row[0]
            except Exception:
                pass
    return counts


def row_to_decision(row: dict) -> AIDecision:
    """Maps a SQLite Row dictionary to a Pydantic AIDecision model."""
    outcome_rec = row["outcome_recorded_at"]
    outcome_recorded_at = datetime.fromisoformat(outcome_rec) if outcome_rec else None

    return AIDecision(
        decision_id=row["decision_id"],
        session_id=row["session_id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        provider=row["provider"],
        model_id=row["model_id"],
        task_type=TaskType(row["task_type"]),
        suggestion_summary=row["suggestion_summary"],
        suggestion_hash=row["suggestion_hash"],
        user_action=UserAction(row["user_action"]),
        modification_summary=row["modification_summary"],
        outcome=DecisionOutcome(row["outcome"]),
        outcome_recorded_at=outcome_recorded_at,
        confidence_score=row["confidence_score"],
        routing_metadata=json.loads(row["routing_metadata"]),
    )


async def insert_decision(db_path: str, decision: AIDecision) -> None:
    """Inserts a new AIDecision log record into the database.

    CRITICAL IMMUTABILITY RULE:
    The original decision payload (such as session_id, provider, model_id, task_type,
    suggestion_summary, suggestion_hash, and user_action) is WRITE-ONCE and must never 
    be updated or modified after creation. Only deferred outcome fields may change.
    """
    # Pre-serialize and check valid JSON representation
    routing_metadata_str = json.dumps(decision.routing_metadata)
    timestamp_str = decision.timestamp.isoformat()
    outcome_recorded_at_str = (
        decision.outcome_recorded_at.isoformat()
        if decision.outcome_recorded_at
        else None
    )
    created_at_str = datetime.now(timezone.utc).isoformat()

    query = """
        INSERT INTO ai_decisions (
            decision_id, session_id, project_id, task_id, timestamp,
            provider, model_id, task_type, suggestion_summary, suggestion_hash,
            user_action, modification_summary, outcome, outcome_recorded_at,
            confidence_score, routing_metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, json(?), ?)
    """
    async with get_db_conn(db_path) as db:
        await db.execute(
            query,
            (
                decision.decision_id,
                decision.session_id,
                decision.project_id,
                decision.task_id,
                timestamp_str,
                decision.provider,
                decision.model_id,
                decision.task_type.value,
                decision.suggestion_summary,
                decision.suggestion_hash,
                decision.user_action.value,
                decision.modification_summary,
                decision.outcome.value,
                outcome_recorded_at_str,
                decision.confidence_score,
                routing_metadata_str,
                created_at_str,
            ),
        )
        await db.commit()


async def get_decision_by_id(db_path: str, decision_id: str) -> AIDecision | None:
    """Retrieves a single AIDecision by its primary key ID."""
    query = "SELECT * FROM ai_decisions WHERE decision_id = ?"
    async with get_db_conn(db_path) as db:
        async with db.execute(query, (decision_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row_to_decision(dict(row))
    return None


async def update_decision_outcome(
    db_path: str,
    decision_id: str,
    outcome: DecisionOutcome,
    outcome_recorded_at: datetime,
) -> AIDecision | None:
    """Updates the outcome and outcome_recorded_at fields of an existing decision.

    CRITICAL IMMUTABILITY RULE:
    Only deferred outcome fields (`outcome` and `outcome_recorded_at`) can be updated.
    All other fields (e.g. suggestion_summary, provider, model_id, etc.) are write-once
    and strictly immutable to maintain log audit integrity.
    """
    query = """
        UPDATE ai_decisions
        SET outcome = ?, outcome_recorded_at = ?
        WHERE decision_id = ?
    """
    async with get_db_conn(db_path) as db:
        async with db.execute(
            query,
            (outcome.value, outcome_recorded_at.isoformat(), decision_id),
        ) as cursor:
            if cursor.rowcount == 0:
                return None
        await db.commit()
    return await get_decision_by_id(db_path, decision_id)


async def list_recent_decisions(db_path: str, limit: int = 20) -> list[AIDecision]:
    """Lists the most recently recorded decisions ordered by timestamp descending."""
    query = "SELECT * FROM ai_decisions ORDER BY timestamp DESC LIMIT ?"
    decisions = []
    async with get_db_conn(db_path) as db:
        async with db.execute(query, (limit,)) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                decisions.append(row_to_decision(dict(row)))
    return decisions


async def find_recent_duplicate_decision(
    db_path: str,
    suggestion_hash: str,
    session_id: str,
    provider: str,
    model_id: str,
    user_action: UserAction,
    window_seconds: int = 10,
    reference_time: datetime | None = None,
) -> AIDecision | None:
    """Finds if a matching decision exists within a short time window to guard against duplicates."""
    ref = reference_time or datetime.now(timezone.utc)
    cutoff = ref - timedelta(seconds=window_seconds)
    cutoff_str = cutoff.isoformat()

    query = """
        SELECT * FROM ai_decisions
        WHERE suggestion_hash = ?
          AND session_id = ?
          AND provider = ?
          AND model_id = ?
          AND user_action = ?
          AND timestamp >= ?
        ORDER BY timestamp DESC
        LIMIT 1
    """
    async with get_db_conn(db_path) as db:
        async with db.execute(
            query,
            (
                suggestion_hash,
                session_id,
                provider,
                model_id,
                user_action.value,
                cutoff_str,
            ),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row_to_decision(dict(row))
    return None


async def decision_exists(db_path: str, decision_id: str) -> bool:
    """Checks if an AIDecision record exists with the given decision_id."""
    query = "SELECT 1 FROM ai_decisions WHERE decision_id = ?"
    async with get_db_conn(db_path) as db:
        async with db.execute(query, (decision_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None


def row_to_mistake(row: dict) -> AIMistake:
    """Maps a SQLite Row dictionary to a Pydantic AIMistake model."""
    return AIMistake(
        mistake_id=row["mistake_id"],
        decision_id=row["decision_id"],
        session_id=row["session_id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        provider=row["provider"],
        model_id=row["model_id"],
        task_type=TaskType(row["task_type"]),
        failure_type=FailureType(row["failure_type"]),
        failure_summary=row["failure_summary"],
        correction_made=bool(row["correction_made"]),
        correction_summary=row["correction_summary"],
        severity=Severity(row["severity"]),
        auto_detected=bool(row["auto_detected"]),
        detection_signal=row["detection_signal"],
    )


async def insert_mistake(db_path: str, mistake: AIMistake) -> None:
    """Inserts a new AIMistake log record into the database.

    CRITICAL IMMUTABILITY RULE:
    Logged AI mistakes are audit-style records and are strictly write-once.
    Once created, mistake records must never be modified or deleted.
    """
    timestamp_str = mistake.timestamp.isoformat()
    created_at_str = datetime.now(timezone.utc).isoformat()

    query = """
        INSERT INTO ai_mistakes (
            mistake_id, decision_id, session_id, project_id, task_id, timestamp,
            provider, model_id, task_type, failure_type, failure_summary,
            correction_made, correction_summary, severity, auto_detected,
            detection_signal, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    async with get_db_conn(db_path) as db:
        await db.execute(
            query,
            (
                mistake.mistake_id,
                mistake.decision_id,
                mistake.session_id,
                mistake.project_id,
                mistake.task_id,
                timestamp_str,
                mistake.provider,
                mistake.model_id,
                mistake.task_type.value,
                mistake.failure_type.value,
                mistake.failure_summary,
                1 if mistake.correction_made else 0,
                mistake.correction_summary,
                mistake.severity.value,
                1 if mistake.auto_detected else 0,
                mistake.detection_signal,
                created_at_str,
            ),
        )
        await db.commit()


async def get_mistake_by_id(db_path: str, mistake_id: str) -> AIMistake | None:
    """Retrieves a single AIMistake by its primary key ID."""
    query = "SELECT * FROM ai_mistakes WHERE mistake_id = ?"
    async with get_db_conn(db_path) as db:
        async with db.execute(query, (mistake_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row_to_mistake(dict(row))
    return None


async def list_recent_mistakes(db_path: str, limit: int = 20) -> list[AIMistake]:
    """Lists the most recently recorded mistakes ordered by timestamp descending."""
    query = "SELECT * FROM ai_mistakes ORDER BY timestamp DESC LIMIT ?"
    mistakes = []
    async with get_db_conn(db_path) as db:
        async with db.execute(query, (limit,)) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                mistakes.append(row_to_mistake(dict(row)))
    return mistakes


async def find_recent_duplicate_mistake(
    db_path: str,
    decision_id: str | None,
    session_id: str,
    provider: str,
    model_id: str,
    failure_type: FailureType,
    failure_summary: str,
    window_seconds: int = 30,
    reference_time: datetime | None = None,
) -> AIMistake | None:
    """Checks if a duplicate mistake exists within a recent time window to guard against duplicate entries."""
    ref = reference_time or datetime.now(timezone.utc)
    cutoff = ref - timedelta(seconds=window_seconds)
    cutoff_str = cutoff.isoformat()

    if decision_id:
        query = """
            SELECT * FROM ai_mistakes
            WHERE (decision_id = ? OR (
                session_id = ? AND provider = ? AND model_id = ? AND failure_type = ? AND failure_summary = ?
            ))
              AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 1
        """
        params = (
            decision_id,
            session_id,
            provider,
            model_id,
            failure_type.value,
            failure_summary,
            cutoff_str,
        )
    else:
        query = """
            SELECT * FROM ai_mistakes
            WHERE session_id = ? AND provider = ? AND model_id = ? AND failure_type = ? AND failure_summary = ?
              AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT 1
        """
        params = (
            session_id,
            provider,
            model_id,
            failure_type.value,
            failure_summary,
            cutoff_str,
        )

    async with get_db_conn(db_path) as db:
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if row:
                return row_to_mistake(dict(row))
    return None


def row_to_profile(row: dict) -> ProviderProfile:
    """Maps a SQLite Row dictionary to a Pydantic ProviderProfile model."""
    last_seen = row["last_seen_at"]
    last_seen_at = datetime.fromisoformat(last_seen) if last_seen else None

    return ProviderProfile(
        provider=row["provider"],
        model_id=row["model_id"],
        provider_id=row["provider_id"],
        model_name=row["model_name"],
        task_type=TaskType(row["task_type"]),
        total_decisions=row["total_decisions"],
        successful_decisions=row["successful_decisions"],
        mistake_count=row["mistake_count"],
        user_reported_mistakes=row["user_reported_mistakes"],
        correction_count=row["correction_count"],
        revert_count=row["revert_count"],
        heavy_edit_count=row["heavy_edit_count"],
        build_error_count=row["build_error_count"],
        avg_feedback_score=row["avg_feedback_score"],
        avg_latency_ms=row["avg_latency_ms"],
        success_rate=row["success_rate"],
        acceptance_rate=row["acceptance_rate"],
        modification_rate=row["modification_rate"],
        rejection_rate=row["rejection_rate"],
        revert_rate=row["revert_rate"],
        mistake_rate=row["mistake_rate"],
        correction_rate=row["correction_rate"],
        heavy_edit_rate=row["heavy_edit_rate"],
        build_error_rate=row["build_error_rate"],
        last_seen_at=last_seen_at,
        profile_confidence=row["profile_confidence"],
        confidence_factor=row["confidence_factor"],
        quality_score=row["quality_score"],
        cold_start=bool(row["cold_start"]),
        sample_size_bucket=row["sample_size_bucket"],
        mistake_by_type=json.loads(row["mistake_by_type"]),
        avg_confidence=row["avg_confidence"],
        routing_score=row["routing_score"],
        last_updated=datetime.fromisoformat(row["last_updated"]),
    )


async def upsert_provider_profile(db_path: str, profile: ProviderProfile) -> None:
    """Inserts or replaces a ProviderProfile record in the database."""
    last_seen_str = profile.last_seen_at.isoformat() if profile.last_seen_at else None
    last_updated_str = profile.last_updated.isoformat()
    mistake_by_type_str = json.dumps(profile.mistake_by_type)

    query = """
        INSERT OR REPLACE INTO provider_profiles (
            provider, model_id, provider_id, model_name, task_type,
            total_decisions, successful_decisions, mistake_count,
            user_reported_mistakes, correction_count, revert_count,
            heavy_edit_count, build_error_count, avg_feedback_score,
            avg_latency_ms, success_rate, acceptance_rate, modification_rate,
            rejection_rate, revert_rate, mistake_rate, correction_rate,
            heavy_edit_rate, build_error_rate, last_seen_at, profile_confidence,
            confidence_factor, quality_score, cold_start, sample_size_bucket,
            mistake_by_type, avg_confidence, routing_score, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    async with get_db_conn(db_path) as db:
        await db.execute(
            query,
            (
                profile.provider,
                profile.model_id,
                profile.provider_id,
                profile.model_name,
                profile.task_type.value,
                profile.total_decisions,
                profile.successful_decisions,
                profile.mistake_count,
                profile.user_reported_mistakes,
                profile.correction_count,
                profile.revert_count,
                profile.heavy_edit_count,
                profile.build_error_count,
                profile.avg_feedback_score,
                profile.avg_latency_ms,
                profile.success_rate,
                profile.acceptance_rate,
                profile.modification_rate,
                profile.rejection_rate,
                profile.revert_rate,
                profile.mistake_rate,
                profile.correction_rate,
                profile.heavy_edit_rate,
                profile.build_error_rate,
                last_seen_str,
                profile.profile_confidence,
                profile.confidence_factor,
                profile.quality_score,
                1 if profile.cold_start else 0,
                profile.sample_size_bucket,
                mistake_by_type_str,
                profile.avg_confidence,
                profile.routing_score,
                last_updated_str,
            ),
        )
        await db.commit()


async def get_provider_profile_by_keys(
    db_path: str, provider: str, model_id: str, task_type: str
) -> ProviderProfile | None:
    """Retrieves a single ProviderProfile by its compound primary key keys."""
    query = "SELECT * FROM provider_profiles WHERE provider = ? AND model_id = ? AND task_type = ?"
    async with get_db_conn(db_path) as db:
        async with db.execute(query, (provider, model_id, task_type)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row_to_profile(dict(row))
    return None


async def list_provider_profiles_from_db(
    db_path: str, task_type: str | None = None
) -> list[ProviderProfile]:
    """Lists saved provider profiles, optionally filtered by task type."""
    if task_type:
        query = "SELECT * FROM provider_profiles WHERE task_type = ? ORDER BY quality_score DESC"
        params = (task_type,)
    else:
        query = "SELECT * FROM provider_profiles ORDER BY quality_score DESC"
        params = ()

    profiles = []
    async with get_db_conn(db_path) as db:
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                profiles.append(row_to_profile(dict(row)))
    return profiles
