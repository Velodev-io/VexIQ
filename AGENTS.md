# VexIQ — Agent Context

VexIQ is the internal AI judgment engine for Vexon OS.
It is **NOT** a standalone product.
It is **NOT** a user-facing service.
It logs decisions, tracks mistakes, and routes tasks to better providers over time.

## Core Modules
- `decision_logger.py` — Logs raw decisions and updates deferred outcomes.
- `mistake_tracker.py` — Logs AI mistakes and user/system corrections.
- `detection_signals.py` — Evaluates system events to auto-detect mistakes.
- `provider_profile.py` — Aggregates decision and mistake logs into provider performance summaries.
- `routing_engine.py` — Computes provider routing scores and recommends models.
- `vexctx_sync.py` — Synchronizes decision and mistake events with VexCTX.

## Key Metadata
- **Internal API Port:** `8767`
- **Storage Strategy:** Hot local SQLite + Long-term encrypted VexCTX vault.
- **Service Integration:** Only called by the Vexon OS Provider Connector.
