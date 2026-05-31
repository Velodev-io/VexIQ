"""VexIQ Routing Engine.

Computes weighted scoring equations utilizing provider profiles with exponential time
decay, handling provider selection recommendations and cold start scenarios.
"""

import uuid
from datetime import datetime, timezone
from vexiq.models import RoutingDecision, TaskType, ProviderProfile
from vexiq.core.provider_profile import ProviderProfileBuilder

# Centralized fallback mapping matching Vexon OS ecosystem defaults
DEFAULT_ROUTING_FALLBACKS = {
    "chat": [("openai", "gpt-4.1"), ("anthropic", "claude-3.5-sonnet")],
    "code": [("anthropic", "claude-3.5-sonnet"), ("openai", "gpt-4.1")],
    "code_edit": [("anthropic", "claude-3.5-sonnet"), ("openai", "gpt-4.1")],
    "command": [("openai", "gpt-4.1"), ("ollama", "llama3.2")],
    "architecture": [("anthropic", "claude-3.5-sonnet"), ("openai", "gpt-4.1")],
    "artifact": [("openai", "gpt-4.1"), ("anthropic", "claude-3.5-sonnet")],
    "config": [("openai", "gpt-4.1"), ("ollama", "llama3.2")],
    "other": [("openai", "gpt-4.1")],
}


class RoutingEngine:
    """Service layer that ranks provider models and executes fallback-oriented routing decisions."""

    MIN_CONFIDENCE_FACTOR = 0.20
    MIN_QUALITY_SCORE = 0.35

    def __init__(self, db_path: str):
        """Initializes the RoutingEngine referencing the active SQLite database path."""
        self.db_path = db_path
        self.profile_builder = ProviderProfileBuilder(db_path)

    async def recommend_provider(self, task_type: str) -> RoutingDecision:
        """Returns the recommended routing decision for the given task type."""
        return await self.recommend_with_candidates(task_type, limit=5)

    async def recommend_with_candidates(
        self, task_type: str, limit: int = 5
    ) -> RoutingDecision:
        """Computes routing recommendation using historical profiles, falling back if data is sparse."""
        try:
            tt = TaskType(task_type)
        except ValueError:
            raise ValueError(f"Invalid task_type '{task_type}'")

        profiles = await self.rank_candidates(task_type, limit=limit)

        if not profiles:
            # Fallback Case 1: No profiles exist for task type
            return await self.get_fallback_candidate(task_type)

        top_profile = profiles[0]

        # Determine decision source and fallback parameters
        fallback_used = False
        fallback_reason = None
        decision_source = "profile_ranked"

        # Check if only cold start or low confidence profiles exist
        if (
            top_profile.cold_start
            or (top_profile.confidence_factor or 0.0) < self.MIN_CONFIDENCE_FACTOR
        ):
            # Fallback Case 2: Insufficient history
            fallback_used = True
            decision_source = "low_confidence_fallback"
            fallback_reason = "insufficient_history"
            provider = top_profile.provider
            model = top_profile.model_id
            score = top_profile.quality_score
            profile_confidence = top_profile.profile_confidence
            confidence_factor = top_profile.confidence_factor
            cold_start = True
        # Check if profiles exist but all are poor quality
        elif (top_profile.quality_score or 0.0) < self.MIN_QUALITY_SCORE:
            # Fallback Case 3: Poor quality threshold
            fallback_used = True
            decision_source = "low_quality_fallback"
            fallback_reason = "all_candidates_below_threshold"

            # Fall back to static fallback candidate if configured
            fallbacks = DEFAULT_ROUTING_FALLBACKS.get(tt.value, [])
            if fallbacks:
                provider, model = fallbacks[0]
            else:
                provider = top_profile.provider
                model = top_profile.model_id
            score = 0.30
            profile_confidence = "low"
            confidence_factor = 0.0
            cold_start = True
        else:
            # Warm path
            provider = top_profile.provider
            model = top_profile.model_id
            score = top_profile.quality_score
            profile_confidence = top_profile.profile_confidence
            confidence_factor = top_profile.confidence_factor
            cold_start = top_profile.cold_start

        # Prepare ranked candidates metadata
        ranked_candidates = []
        for i, p in enumerate(profiles):
            reason = "highest_quality_score" if i == 0 else "lower_quality_score"
            if p.cold_start:
                reason = "cold_start_candidate"
            ranked_candidates.append(
                {
                    "provider_id": p.provider,
                    "model_name": p.model_id,
                    "quality_score": p.quality_score,
                    "profile_confidence": p.profile_confidence,
                    "cold_start": p.cold_start,
                    "reason": reason,
                }
            )

        # If low_quality_fallback was used and we switched to a static default,
        # prepend the static default list to ranked_candidates
        if decision_source == "low_quality_fallback":
            fallbacks = DEFAULT_ROUTING_FALLBACKS.get(tt.value, [])
            static_candidates = []
            for i, (p_id, m_name) in enumerate(fallbacks):
                static_candidates.append(
                    {
                        "provider_id": p_id,
                        "model_name": m_name,
                        "quality_score": 0.30,
                        "profile_confidence": "low",
                        "cold_start": True,
                        "reason": (
                            "fallback_priority_1"
                            if i == 0
                            else f"fallback_priority_{i+1}"
                        ),
                    }
                )
            ranked_candidates = static_candidates + ranked_candidates

        return RoutingDecision(
            routing_id=str(uuid.uuid4()),
            task_type=tt,
            selected_provider=provider,
            selected_model=model,
            recommended_provider=provider,
            recommended_model=model,
            score=score,
            quality_score=score,
            profile_confidence=profile_confidence,
            confidence_factor=confidence_factor,
            competing_providers=ranked_candidates,
            ranked_candidates=ranked_candidates,
            cold_start=cold_start,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            decision_source=decision_source,
            timestamp=datetime.now(timezone.utc),
        )

    async def rank_candidates(
        self, task_type: str, limit: int = 5
    ) -> list[ProviderProfile]:
        """Queries and ranks all candidate provider profiles for the given task type."""
        try:
            tt = TaskType(task_type)
        except ValueError:
            raise ValueError(f"Invalid task_type '{task_type}'")

        from vexiq.db import list_provider_profiles_from_db
        profiles = await list_provider_profiles_from_db(
            self.db_path, task_type=tt.value
        )

        def ranking_key(p):
            confidence_rank = (
                0
                if p.profile_confidence == "low"
                else (1 if p.profile_confidence == "medium" else 2)
            )
            return (
                p.quality_score or 0.0,
                confidence_rank,
                p.confidence_factor or 0.0,
                0 if p.cold_start else 1,
                p.total_decisions,
            )

        profiles.sort(key=ranking_key, reverse=True)
        return profiles[:limit]

    async def get_fallback_candidate(self, task_type: str) -> RoutingDecision:
        """Returns a static default fallback RoutingDecision for the given task type."""
        try:
            tt = TaskType(task_type)
        except ValueError:
            tt = TaskType.other

        fallbacks = DEFAULT_ROUTING_FALLBACKS.get(tt.value, [("openai", "gpt-4.1")])
        provider, model = fallbacks[0]

        ranked_candidates = []
        for i, (p_id, m_name) in enumerate(fallbacks):
            ranked_candidates.append(
                {
                    "provider_id": p_id,
                    "model_name": m_name,
                    "quality_score": 0.30,
                    "profile_confidence": "low",
                    "cold_start": True,
                    "reason": (
                        "fallback_priority_1"
                        if i == 0
                        else f"fallback_priority_{i+1}"
                    ),
                }
            )

        return RoutingDecision(
            routing_id=str(uuid.uuid4()),
            task_type=tt,
            selected_provider=provider,
            selected_model=model,
            recommended_provider=provider,
            recommended_model=model,
            score=0.30,
            quality_score=0.30,
            profile_confidence="low",
            confidence_factor=0.0,
            competing_providers=ranked_candidates,
            ranked_candidates=ranked_candidates,
            cold_start=True,
            fallback_used=True,
            fallback_reason="no_profile_data",
            decision_source="cold_start_default",
            timestamp=datetime.now(timezone.utc),
        )

    async def explain_recommendation(self, task_type: str) -> dict:
        """Returns a dict containing the decision logic explanation for the given task type."""
        decision = await self.recommend_with_candidates(task_type, limit=5)
        return {
            "task_type": decision.task_type.value,
            "recommended_provider": decision.recommended_provider,
            "recommended_model": decision.recommended_model,
            "quality_score": decision.quality_score,
            "fallback_used": decision.fallback_used,
            "fallback_reason": decision.fallback_reason,
            "decision_source": decision.decision_source,
            "explanation": (
                f"Selected provider '{decision.recommended_provider}' and model "
                f"'{decision.recommended_model}' via {decision.decision_source} "
                f"(fallback_used: {decision.fallback_used})."
            ),
        }
