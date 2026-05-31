# VexIQ Architecture

VexIQ is designed around a closed-loop local feedback architecture. It monitors, evaluates, and optimizes AI task execution within Vexon OS using three layered subsystems:

1. **Capture Layer (Decision Logger & Mistake Tracker):** Collects raw developer interactions, suggestion outcomes, and system execution signals.
2. **Aggregation Layer (Provider Profiles):** Continuously summarizes logs into structured provider-model-task combinations to build historical performance statistics.
3. **Decision Layer (Routing Engine):** Inspects performance metrics to route incoming tasks to the best-performing provider for the requested task type.

---

## Data Models

VexIQ utilizes four primary structured schemas, defined below in plain English:

### 1. AIDecision
Represents an AI suggestion that was shown to the user or system, and the immediate and deferred actions taken.
- `decision_id`: Unique string identifier for the decision.
- `session_id`: Unique identifier for the current Vexon OS session.
- `project_id`: Path or identifier of the active project.
- `task_id`: Identifier of the specific task or user instruction being handled.
- `timestamp`: Date and time when the suggestion was generated.
- `provider`: General service provider (e.g., `claude`, `gpt-4o`, `ollama`).
- `model_id`: Specific model version string (e.g., `claude-3-5-sonnet`).
- `task_type`: Category of task: `code_edit`, `command`, `config`, `architecture`, `artifact`, or `other`.
- `suggestion_summary`: A short, human-readable description of the suggested action.
- `suggestion_hash`: SHA-256 hash of the suggestion payload for comparison and deduplication.
- `user_action`: Immediate user response: `accepted`, `modified`, or `rejected`.
- `modification_summary`: Structured description of user modifications (if `user_action` was `modified`).
- `outcome`: Long-term deferred status: `kept`, `reverted`, `edited_further`, or `unknown`.
- `outcome_recorded_at`: Date and time when the deferred outcome was finalized.
- `confidence_score`: Score (0.0 to 1.0) reported by the provider (if available).
- `routing_metadata`: Context metadata indicating how this provider was selected (e.g., scores, fallbacks).

### 2. AIMistake
Represents a confirmed or inferred failure of an AI-generated suggestion.
- `mistake_id`: Unique string identifier for the mistake record.
- `decision_id`: Reference linking this mistake back to the original `AIDecision` (if one exists).
- `session_id`: Vexon OS session identifier.
- `project_id`: Path or identifier of the active project.
- `task_id`: Specific task identifier.
- `timestamp`: Date and time when the mistake was detected or flagged.
- `provider`: General service provider responsible for the mistake.
- `model_id`: Specific model version string.
- `task_type`: Task category (matching the `AIDecision` taxonomy).
- `failure_type`: Classification of failure: `wrong_code`, `wrong_command`, `wrong_architecture`, `wrong_config`, `hallucination`, `incomplete_output`, `broken_build`, `test_failure`, `explicit_rejection`, or `other`.
- `failure_summary`: Human-readable summary of what went wrong.
- `correction_made`: Boolean flag indicating if a correction was successfully applied by the user or system.
- `correction_summary`: Human-readable explanation of how the issue was fixed.
- `severity`: Impact category: `low`, `medium`, `high`, or `critical`.
- `auto_detected`: Boolean indicating if VexIQ automatically inferred this mistake from system events.
- `detection_signal`: Identifier of the system signal that triggered the auto-detection.

### 3. ProviderProfile
Aggregated performance profile representing the historical performance of a specific model for a given task type.
- `provider`: General service provider.
- `model_id`: Specific model version string.
- `task_type`: Categorized task type.
- `total_decisions`: Total number of decisions logged for this provider/model/task combination.
- `acceptance_rate`: Ratio of accepted decisions to total decisions.
- `modification_rate`: Ratio of modified decisions to total decisions.
- `rejection_rate`: Ratio of rejected decisions to total decisions.
- `revert_rate`: Ratio of reverted decisions to accepted decisions.
- `mistake_rate`: Ratio of mistakes to total decisions.
- `mistake_by_type`: Key-value dictionary storing count totals per `failure_type`.
- `avg_confidence`: Average confidence score returned by the model.
- `routing_score`: Currently computed score used by the Routing Engine for selection.
- `last_updated`: Timestamp of the last profile recalculation.

### 4. RoutingDecision
A log of which provider was chosen for a given task and the scoring context.
- `routing_id`: Unique string identifier for the routing action.
- `task_type`: The requested task category.
- `selected_provider`: The chosen service provider.
- `selected_model`: The chosen model version.
- `score`: The routing score of the selected model at the moment of selection.
- `competing_providers`: Map of other candidate models and their respective scores at the time.
- `cold_start`: Boolean indicating if VexIQ reverted to fallback priority settings due to lack of historical records.
- `timestamp`: Date and time when the routing decision occurred.

---

## Routing Score Formula

The Routing Engine calculates the performance score of a provider/model for a given task type using the following weighted arithmetic formula:

$$\text{Score} = \text{Acceptance Rate} - (\text{Revert Rate} \times 2.0) - (\text{Mistake Rate} \times 1.5) - (\text{Modification Rate} \times 0.5) + (\text{Avg Confidence Bonus} \times 0.3)$$

### Scoring Mechanics
1. **Time Decay:** Recent decisions and mistakes are weighted more heavily than older records. VexIQ uses an exponential time-decay function based on the configurable half-life (e.g., 30 days).
2. **Scoring Threshold:** A minimum of **5 decisions** (configurable via `VEXIQ_MIN_DECISIONS_BEFORE_SCORING`) is required for a specific provider/model/task combination before scoring calculations become active.
3. **Cold Start:** If no provider meets the minimum decision threshold for a task type, the engine falls back to the configured provider priority order (e.g., `claude`, `gpt-4o`, `ollama/llama3.2`).

---

## Auto-Detection Signals

VexIQ listens to Vexon OS system events and file alterations to automatically infer mistakes without user intervention:

- **`FILE_REVERT`:** Triggered when a file modified by an AI is reverted via git or local history within **60 minutes** of the edit.
- **`BUILD_FAILURE`:** Triggered if a compiler error, syntax failure, or test suite failure is detected within **5 minutes** of applying AI-generated code.
- **`COMMAND_ERROR`:** Triggered if a terminal command suggested by the AI returns a non-zero exit code during execution.
- **`HEAVY_EDIT`:** Triggered when the user modifies more than **50% of the lines** in an AI-generated block within the same editing session.
- **`AGENT_ERROR`:** Triggered when an active agent workflow errors out or encounters a crash traced back to an AI recommendation.
- **`EXPLICIT_FLAG`:** Triggered when the user clicks an explicit thumbs-down or correction option in Vexon OS.

---

## VexCTX Sync Model

VexIQ relies on VexCTX for long-term database survival and cross-machine portability:
- **Event Mapping:** Decisions are serialized and synced to VexCTX as `event_type: ai_decision`. Mistakes are serialized as `event_type: ai_mistake`.
- **Metadata tagging:** Synced entries are tagged with `ai_assisted: true` and encrypted inside the local VexCTX vault.
- **Asynchronous Execution:** Sync operations run asynchronously in background tasks, ensuring that logging and routing lookups never block the main Vexon OS operational thread.

---

## Service Boundaries

- **FastAPI Endpoint:** Exposes a local FastAPI service running on port `8767`.
- **Local SQLite DB:** Hot writes and reads go to a local SQLite database file (e.g., `~/.vexiq/vexiq.db`).
- **Encrypted Storage:** Long-term archival reads and writes are pushed to the local VexCTX vault via HTTP/SDK.
- **Interaction Constraints:**
  - VexIQ is strictly invoked by Vexon OS internal services (principally the Provider Connector).
  - VexIQ does not accept external traffic.
  - VexIQ does not have any direct cloud integrations or telemetry endpoints.
