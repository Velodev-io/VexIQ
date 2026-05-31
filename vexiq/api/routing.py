"""Routing API router.

Exposes endpoints for querying provider recommendations for specific task types
based on historical performance scores.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from vexiq.config import get_settings, Settings
from vexiq.core.routing_engine import RoutingEngine
from vexiq.models import RoutingDecision, TaskType

router = APIRouter()


@router.get("/recommendations", response_model=RoutingDecision)
async def get_recommendations(
    task_type: str = Query(..., description="The type of AI task to route"),
    limit: int = Query(5, description="Limit for candidate list"),
    include_candidates: bool = Query(
        True, description="Whether to include ranked candidates in the output"
    ),
    settings: Settings = Depends(get_settings),
) -> RoutingDecision:
    """Calculates provider and model recommendations for the requested task type."""
    try:
        TaskType(task_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[
                {
                    "loc": ["query", "task_type"],
                    "msg": (
                        "value is not a valid enumeration member; permitted: "
                        f"{', '.join([e.value for e in TaskType])}"
                    ),
                    "type": "type_error.enum",
                }
            ],
        )

    engine = RoutingEngine(settings.vexiq_db_path)
    try:
        decision = await engine.recommend_with_candidates(task_type, limit=limit)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    if not include_candidates:
        decision.ranked_candidates = []
        decision.competing_providers = []

    return decision
