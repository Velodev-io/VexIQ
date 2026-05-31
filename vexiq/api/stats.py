"""Stats API router.

Exposes analytics endpoints summarizing provider performance metrics,
recent decision logs, and tracked mistake lists.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from vexiq.config import get_settings, Settings
from vexiq.core.stats_service import StatsService
from vexiq.models import (
    StatsSummaryResponse,
    ProviderKPI,
    ProviderDetailResponse,
    TaskTypeKPI,
    LeaderboardEntry,
)

router = APIRouter(prefix="/stats")


@router.get("/summary", response_model=StatsSummaryResponse)
async def get_summary(
    settings: Settings = Depends(get_settings),
) -> StatsSummaryResponse:
    """Returns a global summary of VexIQ intelligence, counts, and performance rates."""
    service = StatsService(settings.vexiq_db_path)
    return await service.get_summary()


@router.get("/providers", response_model=list[ProviderKPI])
async def get_providers(
    task_type: str | None = Query(None, description="Filter by task type"),
    min_confidence: float | None = Query(
        None, description="Minimum confidence factor filter"
    ),
    min_quality_score: float | None = Query(
        None, description="Minimum quality score filter"
    ),
    settings: Settings = Depends(get_settings),
) -> list[ProviderKPI]:
    """Returns provider-level KPIs, optionally filtered by task type, quality, and confidence."""
    service = StatsService(settings.vexiq_db_path)
    return await service.get_providers(
        task_type=task_type,
        min_confidence=min_confidence,
        min_quality_score=min_quality_score,
    )


@router.get("/providers/{provider_id}", response_model=ProviderDetailResponse)
async def get_provider_detail(
    provider_id: str,
    settings: Settings = Depends(get_settings),
) -> ProviderDetailResponse:
    """Returns detailed breakdowns and activity metrics for a single provider."""
    service = StatsService(settings.vexiq_db_path)
    # Check if provider exists by loading detail (if decisions == 0 and covered task types == 0, check empty)
    detail = await service.get_provider_detail(provider_id)
    if detail.total_decisions == 0 and detail.number_of_task_types_covered == 0:
        # Provider not found or no history
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider with ID '{provider_id}' has no logged metrics history",
        )
    return detail


@router.get("/task-types", response_model=list[TaskTypeKPI])
async def get_task_types(
    settings: Settings = Depends(get_settings),
) -> list[TaskTypeKPI]:
    """Returns task-type level coverage statistics and quality classifications."""
    service = StatsService(settings.vexiq_db_path)
    return await service.get_task_types()


@router.get("/leaderboard", response_model=list[LeaderboardEntry])
async def get_leaderboard(
    task_type: str | None = Query(None, description="Filter leaderboard by task type"),
    limit: int = Query(10, description="Max candidates limit"),
    settings: Settings = Depends(get_settings),
) -> list[LeaderboardEntry]:
    """Returns ranked candidate list of provider models, breaking ties using confidence metrics."""
    service = StatsService(settings.vexiq_db_path)
    return await service.get_leaderboard(task_type=task_type, limit=limit)


@router.get("/routing-history")
async def get_routing_history(
    settings: Settings = Depends(get_settings),
) -> dict:
    """Future-proof placeholder endpoint for routing history call logs."""
    service = StatsService(settings.vexiq_db_path)
    summary = await service.get_summary()
    return {
        "total_routing_calls": 0,
        "fallback_count": 0,
        "low_confidence_count": 0,
        "average_quality_score": 0.0,
        "breakdown_by_task_type": {},
        "fallback_rate": summary.fallback_rate,
        "low_confidence_rate": summary.low_confidence_rate,
    }
