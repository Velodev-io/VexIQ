# VexIQ
**The self-improving AI judgment engine for Vexon OS.**

VexIQ is an internal Vexon OS intelligence layer that logs AI decisions, tracks AI mistakes, and uses that historical performance data to dynamically route future tasks to the best-performing AI provider per task type. Instead of relying on static configuration, VexIQ creates a closed-loop system where the more Vexon OS is used, the smarter its routing decisions become.

## Why VexIQ Exists

AI models and providers are not uniform; they have varying strengths and weaknesses across different tasks (e.g., writing code, generating commands, structuring configurations, or analyzing project architectures). 
- **The Problem:** Currently, task routing is static, and users have no automated way to know which provider or model performs best for their specific workflows.
- **The Solution:** VexIQ turns raw historical performance and user corrections into actionable, local intelligence, transforming static provider dispatching into dynamic, data-driven routing.

## Architecture Overview

```
User action in Vexon OS
        │
        ▼
Provider Connector dispatch
        │
        ├── VexIQ Decision Logger ──► records outcome
        ├── VexIQ Mistake Tracker ──► records correction
        │
        ▼
VexIQ Provider Profiles
        │
        ▼
VexIQ Routing Engine
        │
        ▼
Smarter provider selection on next dispatch
        │
        ▼
VexCTX Vault (long-term persistence)
```

## Three Core Systems

### 1. AI Decision Logger
Records every AI recommendation that the user acted on inside Vexon OS.
- **When it triggers:** Triggers whenever an AI provider responds and the user begins interacting with or applying the suggestion.
- **What it logs:** Captures details such as the session, project, task, timestamp, provider, model ID, task type taxonomy, confidence score, suggestion summary/hash, and the immediate user action.
- **Outcome tracking:** Monitors the deferred "outcome" of the decision (e.g., whether the suggestion was kept, reverted within a time window, or further edited).

### 2. AI Mistake Tracker
Detects and categorizes instances where the AI generated incorrect output, faulty commands, or broken code.
- **Auto-detection signals:** Automatically triggers when files are reverted shortly after an edit, when builds/tests fail after code generation, or when shell commands return non-zero exit codes.
- **Explicit user flagging:** Allows users to manually flag suggestions as incorrect or unhelpful.
- **Failure taxonomy:** Classifies errors under a structured taxonomy (e.g., `wrong_code`, `hallucination`, `broken_build`) to refine provider profiles.

### 3. VexIQ Routing Engine
Computes performance profiles for each provider and task type to route future tasks to the highest-scoring provider.
- **Provider Profiles:** Aggregates total decisions, acceptance rate, modification rate, revert rate, and mistake rate.
- **Routing Score:** Uses a multi-factor weighted formula with time decay to prioritize consistently high-performing models.
- **Cold Start:** Falls back to a configured default provider priority list when insufficient historical data exists.

## Data Flow

1. **User Action:** The user acts on an AI suggestion. The **Decision Logger** records the event and tracks its deferred outcome.
2. **Outcome Logging:** If the user reverts an edit, the build fails, or they explicitly flag the suggestion, the **Mistake Tracker** records the mistake.
3. **Profile Aggregation:** Periodic or event-driven aggregation processes compile decisions and mistakes per provider and task type into local **Provider Profiles**.
4. **Next Dispatch:** On a subsequent task request, the **Routing Engine** scores the available providers and selects the optimal candidate.
5. **VexCTX Vault Sync:** All events are serialized and synced asynchronously to the encrypted **VexCTX Vault** for long-term persistence and portability.

## Scope and Boundaries

- **Internal Only:** VexIQ is strictly an internal intelligence layer for Vexon OS.
- **No External Deployment:** It runs entirely locally on the user's machine.
- **No User-Facing UI:** Version 1 does not expose a user interface; it operates transparently behind the Vexon OS Provider Connector.
- **No Cloud Dependency:** It stores data locally in a hot SQLite database and persists long-term state locally in VexCTX Vault.

## Tech Stack

- **Python 3.12+**
- **FastAPI** (internal API surface)
- **SQLite** (via `aiosqlite` for hot local storage of decisions, mistakes, and profiles)
- **Pydantic v2** (data validation and configuration)
- **uv** (dependency and package management)
- **VexCTX SDK** (for vault synchronization)

## Project Structure

```text
VexIQ/
├── README.md
├── ARCHITECTURE.md
├── DESIGN.md
├── AGENTS.md
├── BUG_FIX_AGENT.md
├── CODE_REVIEW_AGENT.md
├── pyproject.toml
├── .env.example
├── tests/
│   ├── __init__.py
│   ├── test_decision_logger.py
│   ├── test_mistake_tracker.py
│   ├── test_provider_profile.py
│   ├── test_routing_engine.py
│   └── test_api.py
└── vexiq/
    ├── __init__.py
    ├── main.py
    ├── config.py
    ├── db.py
    ├── models.py
    ├── api/
    │   ├── __init__.py
    │   ├── decisions.py
    │   ├── mistakes.py
    │   ├── routing.py
    │   ├── stats.py
    │   └── health.py
    └── core/
        ├── __init__.py
        ├── decision_logger.py
        ├── mistake_tracker.py
        ├── detection_signals.py
        ├── provider_profile.py
        ├── routing_engine.py
        └── vexctx_sync.py
```

| File | Purpose |
| :--- | :--- |
| `core/decision_logger.py` | Records AI decisions and deferred outcomes |
| `core/mistake_tracker.py` | Records AI mistakes and correction events |
| `core/detection_signals.py` | Auto-detects mistake signals from system events |
| `core/provider_profile.py` | Aggregates performance profiles per provider/task |
| `core/routing_engine.py` | Scores and selects best provider per task type |
| `core/vexctx_sync.py` | Syncs records to VexCTX vault |
| `api/decisions.py` | Internal API for decision CRUD |
| `api/mistakes.py` | Internal API for mistake logging and flagging |
| `api/routing.py` | Internal API for routing queries |
| `api/stats.py` | Internal analytics endpoints |
| `api/health.py` | Health check endpoint |
| `db.py` | SQLite schema and migrations |
| `models.py` | Pydantic models |
| `config.py` | Environment config via `pydantic-settings` |
| `main.py` | FastAPI app entrypoint |

## Getting Started

```bash
cd VexIQ
uv sync
cp .env.example .env
uv run python -m vexiq.main
```

Default Port: `http://127.0.0.1:8767`

## Internal API Overview

| Method | Endpoint | Description | Scope |
| :--- | :--- | :--- | :--- |
| `POST` | `/decisions` | Record an AI decision | Internal |
| `PATCH` | `/decisions/{id}/outcome` | Update deferred outcome | Internal |
| `POST` | `/mistakes` | Record an AI mistake | Internal |
| `POST` | `/mistakes/flag` | Explicit user flag | Internal |
| `GET` | `/routing/recommend` | Get best provider for task type | Internal |
| `GET` | `/stats/providers` | Provider performance summary | Internal |
| `GET` | `/stats/decisions` | Recent decision log | Internal |
| `GET` | `/stats/mistakes` | Recent mistake log | Internal |
| `GET` | `/health` | Health check | None |

## Roadmap

- **v0.1** — Foundational layer (Phase 1: Config, SQLite DB, Health check) [IMPLEMENTED]
            — Decision Logger (Phase 2: Persist decisions, outcomes and endpoint APIs) [IMPLEMENTED]
            — Mistake Tracker core (Phase 3: Persist mistakes, user flags and endpoint APIs) [IMPLEMENTED]
            — Detection signals engine (Phases 4-5) [UPCOMING]
- **v0.2** — Provider Profile builder
- **v0.3** — Routing Engine + Provider Connector integration
- **v0.4** — VexCTX sync
- **v0.5** — Stats dashboard endpoints
- **v1.0** — Full Vexon OS integration

## Relationship to Other Vexon OS Services

- **VexCTX** — The encrypted storage substrate for long-term decisions and mistakes.
- **VexIndex** — Codebase context used during AI task execution.
- **VexIQ** — The judgment layer that learns from outcomes to choose the best models.
