"""Sync API router.

Exposes endpoints to trigger manual sync operations, view sync status checkpoints,
and reset checkpoints to force sync replays.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from vexiq.config import get_settings, Settings
from vexiq.core.vexctx_sync import VexCTXSyncEngine, SyncError

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/all")
async def sync_all_records(
    limit_per_type: int = Query(default=100, ge=1),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Triggers incremental sync of decisions, mistakes, and provider profiles to VexCTX."""
    engine = VexCTXSyncEngine(settings.vexiq_db_path, settings)
    return await engine.sync_all(limit_per_type)


@router.post("/decisions")
async def sync_decisions(
    limit: int = Query(default=100, ge=1),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Triggers manual incremental sync of decisions to VexCTX."""
    engine = VexCTXSyncEngine(settings.vexiq_db_path, settings)
    try:
        count = await engine.sync_decisions(limit)
        return {"status": "success", "synced_count": count}
    except SyncError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )


@router.post("/mistakes")
async def sync_mistakes(
    limit: int = Query(default=100, ge=1),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Triggers manual incremental sync of mistakes to VexCTX."""
    engine = VexCTXSyncEngine(settings.vexiq_db_path, settings)
    try:
        count = await engine.sync_mistakes(limit)
        return {"status": "success", "synced_count": count}
    except SyncError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )


@router.post("/profiles")
async def sync_profiles(
    limit: int = Query(default=100, ge=1),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Triggers manual incremental sync of provider profiles to VexCTX."""
    engine = VexCTXSyncEngine(settings.vexiq_db_path, settings)
    try:
        count = await engine.sync_provider_profiles(limit)
        return {"status": "success", "synced_count": count}
    except SyncError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )


@router.get("/status")
async def get_sync_status(
    settings: Settings = Depends(get_settings),
) -> dict:
    """Returns the current sync checkpoints for decisions, mistakes, and provider profiles."""
    engine = VexCTXSyncEngine(settings.vexiq_db_path, settings)
    return await engine.get_sync_status()


@router.post("/reset")
async def reset_sync_checkpoint(
    record_type: str | None = Query(default=None, description="Type of record checkpoint to reset"),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Resets the sync checkpoints. If record_type is omitted, resets all checkpoints."""
    engine = VexCTXSyncEngine(settings.vexiq_db_path, settings)
    await engine.reset_sync_checkpoint(record_type)
    return {"status": "success", "detail": f"Checkpoint reset complete for: {record_type or 'all'}"}
