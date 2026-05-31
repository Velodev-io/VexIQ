"""VexIQ core engine modules.

Contains internal logic for decision logging, mistake tracking, provider 
profile generation, auto-detection triggers, and VexCTX data syncing.
"""

from vexiq.core.decision_logger import DecisionLogger
from vexiq.core.mistake_tracker import MistakeTracker
from vexiq.core.provider_profile import ProviderProfileBuilder
from vexiq.core.routing_engine import RoutingEngine
from vexiq.core.stats_service import StatsService
from vexiq.core.detection_signals import (
    resolve_decision_id,
    detect_file_revert,
    detect_heavy_edit,
    detect_build_error,
    detect_manual_rewrite,
    detect_immediate_retry,
    detect_test_fix_loop,
    ingest_signals_from_vexon_logs,
)

__all__ = [
    "DecisionLogger",
    "MistakeTracker",
    "ProviderProfileBuilder",
    "RoutingEngine",
    "StatsService",
    "resolve_decision_id",
    "detect_file_revert",
    "detect_heavy_edit",
    "detect_build_error",
    "detect_manual_rewrite",
    "detect_immediate_retry",
    "detect_test_fix_loop",
    "ingest_signals_from_vexon_logs",
]
