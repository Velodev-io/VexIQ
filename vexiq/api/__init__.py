"""VexIQ API subpackage.

Contains FastAPI routers for handling decisions, mistakes, routing,
statistics, and service health checks.
"""

from vexiq.api.health import router as health_router
from vexiq.api.decisions import router as decisions_router
from vexiq.api.mistakes import router as mistakes_router
from vexiq.api.routing import router as routing_router
from vexiq.api.stats import router as stats_router
from vexiq.api.sync import router as sync_router

__all__ = [
    "health_router",
    "decisions_router",
    "mistakes_router",
    "routing_router",
    "stats_router",
    "sync_router",
]
