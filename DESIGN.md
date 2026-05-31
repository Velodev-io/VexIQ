# VexIQ Design Decisions

This document outlines the engineering decisions and trade-offs behind VexIQ.

---

## 1. Storage: Why SQLite Instead of a Heavier Database?

VexIQ utilizes a local SQLite database (via `aiosqlite`) for its primary runtime database.
* **Local-First & Zero Dependency:** Vexon OS runs locally on developer machines. Forcing developers to install PostgreSQL, Redis, or Docker images is a significant barrier to entry. SQLite is self-contained and requires zero configuration.
* **High Performance for Single-User Write Volume:** Because VexIQ is a single-user system monitoring active workspace developer loops, the write and query volumes are low (tens to hundreds of operations per hour). SQLite handles this volume with sub-millisecond response times.
* **Low Resource Footprint:** SQLite does not run as a background daemon; it is accessed via a file-based lock, conserving RAM and CPU cycles for core IDE tasks.

---

## 2. Schema: Why Separate Tables for Decisions and Mistakes?

Decisions and mistakes are normalized into distinct database tables rather than stored as a single schema.
* **Different Lifecycles:** An `AIDecision` is written immediately when a suggestion is served, and is updated only once (when its outcome is resolved). An `AIMistake` can occur hours or days later (e.g., when a bug is found or a file is reverted).
* **Sparse Mappings:** Many decisions will not result in mistakes (i.e. correct suggestions). Forcing all decision records to hold mistake fields would result in highly sparse tables.
* **Decoupled Detection:** The Mistake Tracker listens to file edits and shell commands. These signals might identify mistakes that cannot be easily mapped to a single distinct `decision_id` (e.g., general system degeneration). Separate tables prevent mapping errors from breaking the database write pipelines.

---

## 3. Algorithm: Why a Weighted Formula Instead of an ML Model?

For provider selection, VexIQ uses a deterministic weighted arithmetic formula instead of a machine learning classifier.
* **Zero Training Data (Cold Start):** An ML model would require substantial historical training data before generating logical predictions, rendering it useless on day one.
* **Explainability & Trust:** If a developer asks why a specific provider was selected for a task, the engine can return a transparent, mathematical calculation of its scores. ML embeddings or weights are black boxes.
* **Ease of Tuning:** The user or system operator can easily adjust performance priorities (e.g., increasing `VEXIQ_WEIGHT_REVERT` to penalize unstable models heavily) by updating the `.env` configuration file.

---

## 4. Vault Integration: Why Sync with VexCTX Rather than Owning the Vault?

Instead of implementing its own encryption, filesystem sync, and backup logic, VexIQ pushes records to the local VexCTX vault.
* **Single Source of Truth:** VexCTX acts as the central state engine for Vexon OS workspace state, environment data, and code indexes. Persisting VexIQ events in VexCTX keeps all system snapshots unified.
* **Inherited Security:** VexCTX handles secure encryption at rest and secure retrieval. By delegating storage to VexCTX, VexIQ avoids managing cryptographic keys and secure databases.
* **Portability:** If a developer syncs their VexCTX vault to a new laptop, VexIQ automatically retrieves historical logs to immediately reconstruct provider profiles.

---

## 5. Fallbacks: Why Use a Priority List for Cold Starts?

When a fresh project starts, no routing metrics exist.
* **Non-Blocking Execution:** VexIQ must never delay task execution. If provider profiles lack sufficient data, VexIQ instantly returns the first available provider from the configured priority list.
* **Safe Defaults:** Setting default priorities guarantees that premium models are utilized initially while baseline statistics accumulate safely in the background.

---

## 6. Future Upgrade Paths

As VexIQ evolves, the underlying systems can be upgraded with minimal API disruption:
* **ML-Based Context Routing:** Moving from category-based routing to embedding-based routing (e.g., matching the code snippet semantics to historical mistake patterns).
* **Cross-User Model Profiles:** Safely aggregating anonymized, crowdsourced provider performance profiles across multiple users to bootstrap cold starts.
* **RLHF Loop:** Tight integration with Vexon OS codebase generators to automatically prompt developers for feedback on high-severity mistakes, feeding the data directly into model fine-tuning sets.
