"""Main entrypoint for the VexIQ service.

Initializes the FastAPI application, registers route routers (decisions, mistakes, 
routing, stats, health), sets up database connection pools, and configures middleware.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn

from vexiq.config import get_settings
from vexiq.db import init_db
from vexiq.api.health import router as health_router
from vexiq.api.decisions import router as decisions_router
from vexiq.api.mistakes import router as mistakes_router
from vexiq.api.routing import router as routing_router
from vexiq.api.stats import router as stats_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events manager for the FastAPI application."""
    # Startup actions
    settings = get_settings()
    await init_db(settings.vexiq_db_path)
    yield
    # Shutdown actions (if any)


app = FastAPI(
    title="VexIQ",
    version="0.1.0",
    description="Self-improving AI judgment engine for Vexon OS",
    lifespan=lifespan,
)

# Register routers
app.include_router(health_router)
app.include_router(decisions_router)
app.include_router(mistakes_router)
app.include_router(routing_router)
app.include_router(stats_router)


@app.get("/")
async def read_root() -> dict:
    """Returns the service identifier and descriptor."""
    return {
        "service": "vexiq",
        "version": "0.1.0",
        "description": "Self-improving AI judgment engine for Vexon OS",
    }


if __name__ == "__main__":
    app_settings = get_settings()
    uvicorn.run(
        "vexiq.main:app",
        host="127.0.0.1",
        port=app_settings.vexiq_port,
        log_level=app_settings.vexiq_log_level.lower(),
    )
