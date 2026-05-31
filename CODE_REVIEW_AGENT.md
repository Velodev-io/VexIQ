# VexIQ Code Review Guidelines

Review all incoming pull requests and modifications against the following rules to maintain performance, stability, and security boundaries.

---

## 1. Async & Non-Blocking Design

* **Task Execution Protection:** VexIQ must **never** block the core task execution of Vexon OS. If VexIQ experiences a crash, database lock, or timeout, the caller (Provider Connector) must catch the error immediately and fallback gracefully to default routing.
* **Asynchronous DB Operations:** SQLite queries must always use the asynchronous `aiosqlite` package. Avoid synchronous database calls or standard `sqlite3` driver queries in the HTTP thread loop.

---

## 2. Model & Formula Interpretability

* **Maintain Transparent Logic:** Do not introduce black-box heuristics or complex embedded classifiers to calculate provider scores. 
* **Scoring Rules:** Routing scores must remain mathematically interpretable, relying on explicit, decay-weighted rates (acceptance, revert, mistake, modification, confidence). Any changes to the scoring formula must be documented in `ARCHITECTURE.md` and tunable via environment variables.

---

## 3. VexCTX Sync Requirements

* **Fire-and-Forget:** Syncing decisions and mistakes to VexCTX must occur out-of-band in background tasks (e.g., using FastAPI's `BackgroundTasks` or async queues). The main response payload of a decision/mistake logging request must never block on a VexCTX network transaction.
* **Network Failures:** Inability to communicate with VexCTX must not crash the service; sync failures should log a warning, keep the record in local SQLite, and retry on subsequent cycles.

---

## 4. Security & Network Boundaries

* **No External Outbound Traffic:** VexIQ must not perform any external network requests.
* **Forbidden Imports:** Under no circumstances should packages like `urllib`, `requests`, or `httpx` be imported to talk to external endpoints (non-localhost/non-VexCTX).
* **Localhost Validation:** All outbound HTTP API calls must target `VEXCTX_BASE_URL` on loopback (typically `http://localhost:8765` or `http://127.0.0.1:8765`).
