"""AI Mistake Tracker core engine.

Manages recording occurrences where AI outputs are incorrect or unusable, 
and supports logging explicit user flagging indicators.
"""

from datetime import datetime, timezone
import uuid

from vexiq.models import AIMistake, FlagMistakeRequest, FailureType, Severity
from vexiq.db import (
    insert_mistake,
    get_mistake_by_id,
    list_recent_mistakes,
    find_recent_duplicate_mistake,
    decision_exists,
)


class MistakeTracker:
    """Service layer class for handling AIMistake persistence, retrieval, deduplication

    safeguards, and user flagging requests.

    CRITICAL IMMUTABILITY RULE:
    Logged AI mistakes are audit-style records and are strictly write-once.
    Once a mistake is recorded, its fields must never be modified or deleted.
    """

    def __init__(self, db_path: str):
        """Initializes MistakeTracker with a reference to the active SQLite database path."""
        self.db_path = db_path

    async def create_mistake(self, mistake: AIMistake) -> AIMistake:
        """Evaluates domain rules, deduplication safeguards, and persists the mistake record.

        Args:
            mistake: Fully constructed AIMistake model.

        Raises:
            ValueError: If a non-null decision_id is provided but does not exist.
        """
        # Domain validation: If decision_id is linked, verify it exists in the decisions table
        if mistake.decision_id:
            exists = await decision_exists(self.db_path, mistake.decision_id)
            if not exists:
                raise ValueError(
                    f"Referenced decision_id '{mistake.decision_id}' does not exist"
                )

        # Deduplication safeguard: look for matching mistake in the last 30 seconds
        duplicate = await find_recent_duplicate_mistake(
            self.db_path,
            decision_id=mistake.decision_id,
            session_id=mistake.session_id,
            provider=mistake.provider,
            model_id=mistake.model_id,
            failure_type=mistake.failure_type,
            failure_summary=mistake.failure_summary,
            window_seconds=30,
            reference_time=mistake.timestamp,
        )
        if duplicate:
            return duplicate

        await insert_mistake(self.db_path, mistake)
        return mistake

    async def get_mistake(self, mistake_id: str) -> AIMistake | None:
        """Retrieves a logged mistake by its UUID mistake_id.

        Args:
            mistake_id: The UUID mistake identifier.
        """
        return await get_mistake_by_id(self.db_path, mistake_id)

    async def list_recent_mistakes(self, limit: int = 20) -> list[AIMistake]:
        """Lists recently logged mistake occurrences.

        Args:
            limit: Maximum count of returned records.
        """
        return await list_recent_mistakes(self.db_path, limit)

    async def flag_mistake(
        self, request: FlagMistakeRequest, mistake_id: str | None = None
    ) -> AIMistake:
        """Converts a lightweight user flagging request into a full AIMistake and records it.

        Args:
            request: Lightweight explicit user flag payload.
            mistake_id: Optional pre-generated UUID mistake identifier.
        """
        generated_id = mistake_id or str(uuid.uuid4())
        timestamp = request.timestamp or datetime.now(timezone.utc)
        correction_made = True if request.correction_summary else False

        mistake = AIMistake(
            mistake_id=generated_id,
            decision_id=request.decision_id,
            session_id=request.session_id,
            project_id=request.project_id,
            task_id=request.task_id,
            timestamp=timestamp,
            provider=request.provider,
            model_id=request.model_id,
            task_type=request.task_type,
            failure_type=request.failure_type,
            failure_summary=request.failure_summary,
            correction_made=correction_made,
            correction_summary=request.correction_summary,
            severity=request.severity,
            auto_detected=False,
            detection_signal="EXPLICIT_FLAG",
        )

        return await self.create_mistake(mistake)
