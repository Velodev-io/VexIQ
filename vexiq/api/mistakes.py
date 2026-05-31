"""Mistakes API router.

Exposes endpoints for recording new AI mistakes and processing explicit user flags.
"""

from datetime import datetime, timezone
import uuid
from fastapi import APIRouter, Depends, HTTPException, Response, status

from vexiq.config import get_settings, Settings
from vexiq.core.mistake_tracker import MistakeTracker
from vexiq.models import (
    AIMistake,
    CreateMistakeRequest,
    FlagMistakeRequest,
)

router = APIRouter()


@router.post("/mistakes", response_model=AIMistake)
async def create_mistake(
    request: CreateMistakeRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> AIMistake:
    """Logs a new AI mistake event or returns an existing duplicate event.

    Returns:
        The logged AIMistake, setting status code to 201 Created for new entries, 
        and 200 OK for deduplicated records.
    """
    tracker = MistakeTracker(settings.vexiq_db_path)

    generated_id = str(uuid.uuid4())
    timestamp = request.timestamp or datetime.now(timezone.utc)

    mistake_model = AIMistake(
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
        correction_made=request.correction_made,
        correction_summary=request.correction_summary,
        severity=request.severity,
        auto_detected=request.auto_detected,
        detection_signal=request.detection_signal,
    )

    try:
        result = await tracker.create_mistake(mistake_model)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    if result.mistake_id != generated_id:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED

    return result


@router.post("/mistakes/flag", response_model=AIMistake)
async def flag_mistake(
    request: FlagMistakeRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> AIMistake:
    """Logs a lightweight user-reported flag mistake event.

    Returns:
        The logged AIMistake, setting status code to 201 Created for new entries, 
        and 200 OK for deduplicated records.
    """
    tracker = MistakeTracker(settings.vexiq_db_path)

    generated_id = str(uuid.uuid4())
    try:
        result = await tracker.flag_mistake(request, mistake_id=generated_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    if result.mistake_id != generated_id:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED

    return result


@router.get("/mistakes/{mistake_id}", response_model=AIMistake)
async def get_mistake(
    mistake_id: str,
    settings: Settings = Depends(get_settings),
) -> AIMistake:
    """Retrieves a logged AI mistake by its UUID identifier."""
    tracker = MistakeTracker(settings.vexiq_db_path)
    mistake = await tracker.get_mistake(mistake_id)
    if not mistake:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mistake with ID '{mistake_id}' not found",
        )
    return mistake


@router.get("/mistakes", response_model=list[AIMistake])
async def list_mistakes(
    limit: int = 20,
    settings: Settings = Depends(get_settings),
) -> list[AIMistake]:
    """Lists the recent AI mistakes logged in the database."""
    tracker = MistakeTracker(settings.vexiq_db_path)
    return await tracker.list_recent_mistakes(limit)
