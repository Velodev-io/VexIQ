# VexIQ Debugging Guidelines

Use these instructions when debugging VexIQ logic, database errors, or API failures.

---

## 1. System Context

VexIQ is Vexon OS's self-improving intelligence layer. It logs decisions, tracks mistakes, compiles provider statistics, and recommends optimal model routes. It is a local service communicating with VexCTX for persistent state.

---

## 2. Relevant Files to Check First

* **Scoring & Routing Issues:** 
  * Check [routing_engine.py](file:///Users/yashswisingh/Sur Projects/VexIQ/vexiq/core/routing_engine.py) (scoring formula, weight decay, cold start fallbacks).
  * Check [provider_profile.py](file:///Users/yashswisingh/Sur Projects/VexIQ/vexiq/core/provider_profile.py) (log aggregation, profile builders).
* **Mistake & Signal Issues:**
  * Check [detection_signals.py](file:///Users/yashswisingh/Sur Projects/VexIQ/vexiq/core/detection_signals.py) (revert windows, heavy edit thresholds, build failure detectors).
  * Check [mistake_tracker.py](file:///Users/yashswisingh/Sur Projects/VexIQ/vexiq/core/mistake_tracker.py) (mistake CRUD, API validation).
* **API or Request Flow Issues:**
  * Check [main.py](file:///Users/yashswisingh/Sur Projects/VexIQ/vexiq/main.py) (routing, startup hooks, SQLite pool creation).
  * Check [api/](file:///Users/yashswisingh/Sur Projects/VexIQ/vexiq/api/) files (endpoints, request payloads, response serialization).

---

## 3. Common Bug Areas

* **Routing Score Calculations:** Floating point division by zero errors on new models (ensure checks for zero decisions/attempts exist). Incorrect decay weights when converting intervals.
* **Deferred Outcome Updates:** Tasks marked `unknown` because the outcome-monitoring background worker missed events or failed to map an update to the correct UUID.
* **Detection Signal False Positives:** `FILE_REVERT` triggering on manual git branch swaps, or `HEAVY_EDIT` triggering when auto-formatting files. Look for signal-filtering issues.
* **Database Lock Conflicts:** In `db.py` or connection setup, because SQLite does not support concurrent write transactions out-of-the-box (verify WAL mode is active and connections are async).

---

## 4. Protected Zones (What to NEVER Touch Unassisted)

> [!WARNING]
> Do not modify [vexctx_sync.py](file:///Users/yashswisingh/Sur Projects/VexIQ/vexiq/core/vexctx_sync.py) or VexCTX payload serialization without first consulting the VexCTX API and Vault schema documentation. Broken sync serialization can corrupt or lock the user's secure vault storage, impacting both VexIQ and Vexon OS as a whole.
