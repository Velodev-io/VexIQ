"""VexCTX Sync Engine.

Asynchronously serializes local SQLite decision, mistake, and provider profile entries and
syncs them as event payloads into the long-term encrypted VexCTX vault.
"""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

import httpx

from vexiq.config import Settings, get_settings
from vexiq.db import (
    get_db_conn,
    get_sync_checkpoint,
    update_sync_checkpoint,
    reset_sync_checkpoints,
    row_to_decision,
    row_to_mistake,
    row_to_profile,
)

logger = logging.getLogger("vexiq.sync")


class SyncError(Exception):
    """Base exception for synchronization errors."""
    pass


class VexCTXSyncEngine:
    """Core synchronization engine to export SQLite records to VexCTX."""

    def __init__(self, db_path: str, settings: Settings | None = None):
        self.db_path = db_path
        self.settings = settings or get_settings()

    async def _send_payload(self, record_type: str, records: list[dict[str, Any]]) -> None:
        """Helper to send a batch of records to the VexCTX sync endpoint.

        Raises SyncError on failure.
        """
        if not self.settings.vexiq_vexctx_sync_enabled:
            logger.info("VexCTX sync is disabled in config.")
            return

        payload = {
            "source": "vexiq",
            "record_type": record_type,
            "records": records,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

        headers = {"Content-Type": "application/json"}
        if self.settings.vexctx_api_key:
            headers["Authorization"] = f"Bearer {self.settings.vexctx_api_key}"

        url = f"{self.settings.vexctx_base_url}/sync"
        attempts = max(1, self.settings.vexiq_sync_retry_attempts)
        timeout = httpx.Timeout(self.settings.vexiq_sync_timeout_seconds)

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, attempts + 1):
                try:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    logger.info(
                        "Successfully synced %d %s records to VexCTX.",
                        len(records),
                        record_type,
                    )
                    return
                except (httpx.HTTPError, httpx.StreamError) as e:
                    logger.warning(
                        "Attempt %d/%d to sync %s records failed: %s",
                        attempt,
                        attempts,
                        record_type,
                        str(e),
                    )
                    if attempt == attempts:
                        raise SyncError(
                            f"Failed to sync {record_type} records after {attempts} attempts. Error: {e}"
                        ) from e
                    # Exponential backoff
                    await asyncio.sleep(2 ** attempt * 0.1)

    async def sync_decisions(self, limit: int | None = None) -> int:
        """Syncs unsynced decisions to VexCTX in batch. Returns the number of synced records."""
        batch_size = limit if limit is not None else self.settings.vexiq_sync_batch_size
        if batch_size <= 0:
            return 0

        checkpoint = await get_sync_checkpoint(self.db_path, "decisions")
        
        if checkpoint:
            last_ts, last_id = checkpoint
            query = """
                SELECT * FROM ai_decisions
                WHERE created_at > ? OR (created_at = ? AND decision_id > ?)
                ORDER BY created_at ASC, decision_id ASC
                LIMIT ?
            """
            params = (last_ts, last_ts, last_id, batch_size)
        else:
            query = """
                SELECT * FROM ai_decisions
                ORDER BY created_at ASC, decision_id ASC
                LIMIT ?
            """
            params = (batch_size,)

        records_to_sync = []
        raw_rows = []
        async with get_db_conn(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    raw_rows.append(dict(row))

        if not raw_rows:
            return 0

        for row in raw_rows:
            decision = row_to_decision(row)
            # context_snapshot contains extra fields not in base serialized shape
            context_snapshot = {
                "project_id": decision.project_id,
                "task_id": decision.task_id,
                "suggestion_summary": decision.suggestion_summary,
                "suggestion_hash": decision.suggestion_hash,
                "modification_summary": decision.modification_summary,
                "outcome": decision.outcome.value,
                "outcome_recorded_at": decision.outcome_recorded_at.isoformat() if decision.outcome_recorded_at else None,
                "confidence_score": decision.confidence_score,
                "routing_metadata": decision.routing_metadata,
            }
            records_to_sync.append({
                "decision_id": decision.decision_id,
                "session_id": decision.session_id,
                "task_type": decision.task_type.value,
                "provider_used": decision.provider,
                "model_used": decision.model_id,
                "action_taken": decision.user_action.value,
                "context_snapshot": context_snapshot,
                "timestamp": decision.timestamp.isoformat(),
            })

        await self._send_payload("decisions", records_to_sync)

        # Advance checkpoint
        last_record = raw_rows[-1]
        await update_sync_checkpoint(
            self.db_path,
            "decisions",
            last_record["created_at"],
            last_record["decision_id"],
        )

        return len(records_to_sync)

    async def sync_mistakes(self, limit: int | None = None) -> int:
        """Syncs unsynced mistakes to VexCTX in batch. Returns the number of synced records."""
        batch_size = limit if limit is not None else self.settings.vexiq_sync_batch_size
        if batch_size <= 0:
            return 0

        checkpoint = await get_sync_checkpoint(self.db_path, "mistakes")

        if checkpoint:
            last_ts, last_id = checkpoint
            query = """
                SELECT * FROM ai_mistakes
                WHERE created_at > ? OR (created_at = ? AND mistake_id > ?)
                ORDER BY created_at ASC, mistake_id ASC
                LIMIT ?
            """
            params = (last_ts, last_ts, last_id, batch_size)
        else:
            query = """
                SELECT * FROM ai_mistakes
                ORDER BY created_at ASC, mistake_id ASC
                LIMIT ?
            """
            params = (batch_size,)

        records_to_sync = []
        raw_rows = []
        async with get_db_conn(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    raw_rows.append(dict(row))

        if not raw_rows:
            return 0

        for row in raw_rows:
            mistake = row_to_mistake(row)
            
            # Map mistake fields
            records_to_sync.append({
                "mistake_id": mistake.mistake_id,
                "decision_id": mistake.decision_id,
                "outcome_type": mistake.outcome_type or mistake.failure_type.value,
                "user_corrected": mistake.user_corrected if mistake.user_corrected is not None else mistake.correction_made,
                "correction_detail": mistake.correction_detail or mistake.correction_summary,
                "feedback_signal": mistake.feedback_signal if mistake.feedback_signal is not None else (1.0 if mistake.correction_made else 0.0),
                "is_duplicate": mistake.is_duplicate,
                "timestamp": mistake.timestamp.isoformat(),
                # Extra context fields helpful for vault storage
                "session_id": mistake.session_id,
                "project_id": mistake.project_id,
                "task_id": mistake.task_id,
                "provider": mistake.provider,
                "model_id": mistake.model_id,
                "task_type": mistake.task_type.value,
                "failure_summary": mistake.failure_summary,
                "severity": mistake.severity.value,
                "auto_detected": mistake.auto_detected,
                "detection_signal": mistake.detection_signal,
            })

        await self._send_payload("mistakes", records_to_sync)

        # Advance checkpoint
        last_record = raw_rows[-1]
        await update_sync_checkpoint(
            self.db_path,
            "mistakes",
            last_record["created_at"],
            last_record["mistake_id"],
        )

        return len(records_to_sync)

    async def sync_provider_profiles(self, limit: int | None = None) -> int:
        """Syncs updated provider profiles to VexCTX in batch. Returns the number of synced records."""
        batch_size = limit if limit is not None else self.settings.vexiq_sync_batch_size
        if batch_size <= 0:
            return 0

        checkpoint = await get_sync_checkpoint(self.db_path, "provider_profiles")

        if checkpoint:
            last_ts, last_id = checkpoint
            query = """
                SELECT *, (provider || '|' || model_id || '|' || task_type) as compound_id
                FROM provider_profiles
                WHERE last_updated > ? OR (last_updated = ? AND compound_id > ?)
                ORDER BY last_updated ASC, provider ASC, model_id ASC, task_type ASC
                LIMIT ?
            """
            params = (last_ts, last_ts, last_id, batch_size)
        else:
            query = """
                SELECT *, (provider || '|' || model_id || '|' || task_type) as compound_id
                FROM provider_profiles
                ORDER BY last_updated ASC, provider ASC, model_id ASC, task_type ASC
                LIMIT ?
            """
            params = (batch_size,)

        records_to_sync = []
        raw_rows = []
        async with get_db_conn(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    raw_rows.append(dict(row))

        if not raw_rows:
            return 0

        for row in raw_rows:
            profile = row_to_profile(row)
            
            # Map standard profile fields
            records_to_sync.append({
                "provider_id": profile.provider_id,
                "model_name": profile.model_name,
                "task_type": profile.task_type.value,
                "quality_score": profile.quality_score,
                "profile_confidence": profile.profile_confidence,
                "confidence_factor": profile.confidence_factor,
                "success_rate": profile.success_rate,
                "mistake_rate": profile.mistake_rate,
                "correction_rate": profile.correction_rate,
                "total_decisions": profile.total_decisions,
                "last_seen_at": profile.last_seen_at.isoformat() if profile.last_seen_at else None,
                "cold_start": profile.cold_start,
                # Extra aggregation details
                "provider": profile.provider,
                "model_id": profile.model_id,
                "successful_decisions": profile.successful_decisions,
                "mistake_count": profile.mistake_count,
                "user_reported_mistakes": profile.user_reported_mistakes,
                "correction_count": profile.correction_count,
                "revert_count": profile.revert_count,
                "heavy_edit_count": profile.heavy_edit_count,
                "build_error_count": profile.build_error_count,
                "avg_feedback_score": profile.avg_feedback_score,
                "avg_latency_ms": profile.avg_latency_ms,
                "acceptance_rate": profile.acceptance_rate,
                "modification_rate": profile.modification_rate,
                "rejection_rate": profile.rejection_rate,
                "revert_rate": profile.revert_rate,
                "heavy_edit_rate": profile.heavy_edit_rate,
                "build_error_rate": profile.build_error_rate,
                "sample_size_bucket": profile.sample_size_bucket,
                "mistake_by_type": profile.mistake_by_type,
                "avg_confidence": profile.avg_confidence,
                "routing_score": profile.routing_score,
                "last_updated": profile.last_updated.isoformat(),
            })

        await self._send_payload("provider_profiles", records_to_sync)

        # Advance checkpoint
        last_record = raw_rows[-1]
        await update_sync_checkpoint(
            self.db_path,
            "provider_profiles",
            last_record["last_updated"],
            last_record["compound_id"],
        )

        return len(records_to_sync)

    async def sync_all(self, limit_per_type: int = 100) -> dict[str, Any]:
        """Syncs all record types in order and returns structured status report."""
        status = {
            "decisions": {"status": "success", "synced_count": 0, "error": None},
            "mistakes": {"status": "success", "synced_count": 0, "error": None},
            "provider_profiles": {"status": "success", "synced_count": 0, "error": None},
        }

        # Sync decisions
        try:
            status["decisions"]["synced_count"] = await self.sync_decisions(limit_per_type)
        except Exception as e:
            logger.error("Failed to sync decisions: %s", e)
            status["decisions"]["status"] = "failed"
            status["decisions"]["error"] = str(e)

        # Sync mistakes
        try:
            status["mistakes"]["synced_count"] = await self.sync_mistakes(limit_per_type)
        except Exception as e:
            logger.error("Failed to sync mistakes: %s", e)
            status["mistakes"]["status"] = "failed"
            status["mistakes"]["error"] = str(e)

        # Sync provider profiles
        try:
            status["provider_profiles"]["synced_count"] = await self.sync_provider_profiles(limit_per_type)
        except Exception as e:
            logger.error("Failed to sync provider profiles: %s", e)
            status["provider_profiles"]["status"] = "failed"
            status["provider_profiles"]["error"] = str(e)

        return status

    async def get_sync_status(self) -> dict[str, Any]:
        """Returns the current sync checkpoint status."""
        decisions_cp = await get_sync_checkpoint(self.db_path, "decisions")
        mistakes_cp = await get_sync_checkpoint(self.db_path, "mistakes")
        profiles_cp = await get_sync_checkpoint(self.db_path, "provider_profiles")

        return {
            "decisions": {
                "last_synced_timestamp": decisions_cp[0] if decisions_cp else None,
                "last_synced_id": decisions_cp[1] if decisions_cp else None,
            },
            "mistakes": {
                "last_synced_timestamp": mistakes_cp[0] if mistakes_cp else None,
                "last_synced_id": mistakes_cp[1] if mistakes_cp else None,
            },
            "provider_profiles": {
                "last_synced_timestamp": profiles_cp[0] if profiles_cp else None,
                "last_synced_id": profiles_cp[1] if profiles_cp else None,
            },
        }

    async def reset_sync_checkpoint(self, record_type: str | None = None) -> None:
        """Resets sync cursor checkpoint(s) to force replay/re-sync."""
        await reset_sync_checkpoints(self.db_path, record_type)
