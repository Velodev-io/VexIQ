"""Health Check API router.

Exposes a simple health status check endpoint (`/health`) to monitor service availability.
"""

from fastapi import APIRouter, Depends
from vexiq.config import get_settings, Settings
from vexiq.db import table_exists, get_table_counts

router = APIRouter()


@router.get("/health")
async def health_check(settings: Settings = Depends(get_settings)) -> dict:
    """Returns the operational status of the service, including database initialization

    details and current table sizes.
    """
    db_initialized = await table_exists(settings.vexiq_db_path, "ai_decisions")
    table_counts = (
        await get_table_counts(settings.vexiq_db_path) if db_initialized else {}
    )

    return {
        "status": "ok",
        "service": "vexiq",
        "db_initialized": db_initialized,
        "db_path": settings.vexiq_db_path,
        "table_counts": table_counts,
    }
