"""Decisions API router.

Exposes endpoints for recording new AI suggestions (decisions) and updating 
deferred decision outcomes (e.g., whether the suggestion was kept, modified, or reverted).
"""

from datetime import datetime, timezone
import uuid
from fastapi import APIRouter, Depends, HTTPException, Response, status

from vexiq.config import get_settings, Settings
from vexiq.core.decision_logger import DecisionLogger
from vexiq.models import (
    AIDecision,
    CreateDecisionRequest,
    UpdateDecisionOutcomeRequest,
    DecisionOutcome,
)

router = APIRouter()


@router.post("/decisions", response_model=AIDecision)
async def create_decision(
    request: CreateDecisionRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> AIDecision:
    """Logs a new AI decision event or returns an existing duplicate event.

    Returns:
        The logged AIDecision, setting status code to 201 Created for new entries, 
        and 200 OK for deduplicated records.
    """
    logger = DecisionLogger(settings.vexiq_db_path)

    generated_id = str(uuid.uuid4())
    timestamp = request.timestamp or datetime.now(timezone.utc)

    decision_model = AIDecision(
        decision_id=generated_id,
        session_id=request.session_id,
        project_id=request.project_id,
        task_id=request.task_id,
        timestamp=timestamp,
        provider=request.provider,
        model_id=request.model_id,
        task_type=request.task_type,
        suggestion_summary=request.suggestion_summary,
        suggestion_hash=request.suggestion_hash,
        user_action=request.user_action,
        modification_summary=request.modification_summary,
        outcome=DecisionOutcome.unknown,
        outcome_recorded_at=None,
        confidence_score=request.confidence_score,
        routing_metadata=request.routing_metadata,
    )

    result = await logger.create_decision(decision_model)
    if result.decision_id != generated_id:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED

    return result


@router.get("/decisions/{decision_id}", response_model=AIDecision)
async def get_decision(
    decision_id: str,
    settings: Settings = Depends(get_settings),
) -> AIDecision:
    """Retrieves a logged AI decision by its UUID identifier."""
    logger = DecisionLogger(settings.vexiq_db_path)
    decision = await logger.get_decision(decision_id)
    if not decision:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Decision with ID '{decision_id}' not found",
        )
    return decision


@router.patch("/decisions/{decision_id}/outcome", response_model=AIDecision)
async def update_outcome(
    decision_id: str,
    request: UpdateDecisionOutcomeRequest,
    settings: Settings = Depends(get_settings),
) -> AIDecision:
    """Updates the deferred outcome details for a logged AI decision."""
    logger = DecisionLogger(settings.vexiq_db_path)
    updated = await logger.update_outcome(
        decision_id,
        outcome=request.outcome,
        outcome_recorded_at=request.outcome_recorded_at,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Decision with ID '{decision_id}' not found",
        )
    return updated


@router.get("/decisions", response_model=list[AIDecision])
async def list_decisions(
    limit: int = 20,
    settings: Settings = Depends(get_settings),
) -> list[AIDecision]:
    """Lists the recent AI decisions logged in the database."""
    logger = DecisionLogger(settings.vexiq_db_path)
    return await logger.list_recent_decisions(limit)
