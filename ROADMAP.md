# VexIQ Development Roadmap

This document outlines the two-part architecture and execution plan for VexIQ, a local-first system designed to capture developer actions and mistakes, evaluate model performance, and dynamically route AI requests in Vexon OS.

---

## Architecture Breakdown

### Part 1: VexIQ Capture (Data Recorder)
A structured event recorder focused purely on data capture without scoring or routing logic.
- **AIDecision Logging:** Records details when a user accepts, modifies, or rejects suggestions.
- **AIMistake Logging:** Tracks model failures and corrections.
- **Auto-Detection Signals:** Evaluates system events to trigger mistake records.
- **User Flagging:** Supports manual flagging of incorrect outputs.
- **VexCTX Vault Sync:** Asynchronously serializes and saves records to VexCTX.
- **Goal:** Accumulate high-fidelity feedback data (ideally several hundred decisions) before enabling automation.

### Part 2: VexIQ Intelligence (Decision Brain)
The scoring and routing engine. Reads accumulated Part 1 data to optimize model dispatching.
- **Provider Profiles:** Summarizes raw metrics per provider, model, and task type.
- **Routing Engine:** Computes rankings via a multi-factor weighted formula with time decay.
- **Cold Start Fallbacks:** Reverts to a configured priority list if data is sparse.
- **Stats Dashboard:** Exposes endpoints summarizing performance history.

---

## Execution Phases

### Phase 1 — Part 1 Core: Data Models & DB
- **Scope:** 
  - `models.py` — Pydantic definitions for `AIDecision`, `AIMistake`, and `RoutingDecision`.
  - `db.py` — SQLite schema initialization, migrations, and basic CRUD.
  - `config.py` — `pydantic-settings` setup.
- **Verification:** SQLite tables created on startup; model serialization tested.

### Phase 2 — Part 1 Core: Decision Logger
- **Scope:**
  - `core/decision_logger.py` — full logic including deduplication via `suggestion_hash`.
  - `api/decisions.py` — endpoints for `POST /decisions` and `PATCH /decisions/{id}/outcome`.
- **Verification:** Decision created on `POST`; outcome updated on `PATCH`.

### Phase 3 — Part 1 Core: Mistake Tracker & Detection Signals
- **Scope:**
  - `core/mistake_tracker.py` — logging details and explicit flags.
  - `core/detection_signals.py` — implementation of 6 auto-detection signals (file revert, build failure, command error, heavy edit, agent error, explicit flag).
  - `api/mistakes.py` — endpoints for logging and flagging mistakes.
- **Verification:** Explicit flags log records; file reverts and command errors auto-create mistakes.

### Phase 4 — Part 1 Core: VexCTX Sync
- **Scope:**
  - `core/vexctx_sync.py` — async, non-blocking sync engine.
  - Wire sync hooks into decision and mistake logging flows.
- **Verification:** Events serialized and pushed to VexCTX; sync failures caught gracefully.

### Phase 5 — Part 1 Complete: Integration & Health
- **Scope:**
  - `main.py` — lifespan management and app startup.
  - `api/health.py` — health check endpoints.
  - Complete integration test suite (`test_api.py`).
- **Verification:** App starts on port `8767`; endpoints verified; 15+ tests pass.

### Phase 6 — Part 2 Core: Provider Profile Builder
- **Scope:**
  - `core/provider_profile.py` — metric aggregation and time decay weighting.
  - `db.py` schema updates for profile persistence.
- **Verification:** Profiles correctly calculate acceptance, modification, revert, and mistake rates.

### Phase 7 — Part 2 Core: Routing Engine
- **Scope:**
  - `core/routing_engine.py` — scoring formula, priority fallbacks, decision logger.
  - `api/routing.py` — endpoint for `GET /routing/recommend`.
- **Verification:** Recommendations return highest-performing provider; cold start priority logic operates.

### Phase 8 — Part 2 Complete: Stats & Connector Integration
- **Scope:**
  - `api/stats.py` — analytic query endpoints.
  - Connector client integration with Vexon OS Provider Connector.
- **Verification:** Stats return compiled arrays; connector queries routing recommendations before dispatch.

### Phase 9 — Polish & Hardening
- **Scope:**
  - Error boundaries, local database concurrency locks resolution, VexCTX offline handling, and documentation updates.
