"""Provider Profile Aggregator.

Compiles provider and model performance summaries across different task categories,
calculating aggregates like acceptance, revert, modification, and mistake rates.
"""

import json
from datetime import datetime, timezone
from vexiq.models import ProviderProfile, TaskType
from vexiq.db import get_db_conn, upsert_provider_profile, get_provider_profile_by_keys


class ProviderProfileBuilder:
    """Service layer for building, aggregating, and retrieving provider performance profiles."""

    def __init__(self, db_path: str):
        """Initializes the Builder with a reference to the active SQLite database path."""
        self.db_path = db_path

    async def build_provider_profile(
        self, provider_id: str, model_name: str, task_type: str
    ) -> ProviderProfile:
        """Computes the profile for a single provider-model-task type combination.

        Returns a cold-start profile with safe defaults if no database entries exist.
        """
        try:
            tt = TaskType(task_type)
        except ValueError:
            tt = TaskType.other

        profiles = await self._compute_profiles(
            provider_id=provider_id, model_name=model_name, task_type=task_type
        )
        if profiles:
            return profiles[0]

        # Return default cold-start profile
        return ProviderProfile(
            provider=provider_id,
            model_id=model_name,
            provider_id=provider_id,
            model_name=model_name,
            task_type=tt,
            total_decisions=0,
            successful_decisions=0,
            mistake_count=0,
            user_reported_mistakes=0,
            correction_count=0,
            revert_count=0,
            heavy_edit_count=0,
            build_error_count=0,
            avg_feedback_score=None,
            avg_latency_ms=None,
            success_rate=0.0,
            acceptance_rate=0.0,
            modification_rate=0.0,
            rejection_rate=0.0,
            revert_rate=0.0,
            mistake_rate=0.0,
            correction_rate=0.0,
            heavy_edit_rate=0.0,
            build_error_rate=0.0,
            last_seen_at=None,
            profile_confidence="low",
            confidence_factor=0.0,
            quality_score=0.30,  # Baseline default
            cold_start=True,
            sample_size_bucket="tiny",
            mistake_by_type={},
            avg_confidence=None,
            routing_score=0.30,
            last_updated=datetime.now(timezone.utc),
        )

    async def build_all_provider_profiles(
        self, task_type: str | None = None
    ) -> list[ProviderProfile]:
        """Aggregates and builds profiles for all unique combinations, optionally filtered by task type."""
        return await self._compute_profiles(task_type=task_type)

    async def get_top_profiles(
        self, task_type: str, limit: int = 5
    ) -> list[ProviderProfile]:
        """Returns provider profiles for a given task type sorted by quality_score descending."""
        profiles = await self.build_all_provider_profiles(task_type=task_type)
        profiles.sort(key=lambda p: p.quality_score, reverse=True)
        return profiles[:limit]

    async def refresh_provider_profiles(self) -> int:
        """Recalculates all profiles and caches them in the database.

        Returns the count of refreshed profiles.
        """
        profiles = await self.build_all_provider_profiles()
        for profile in profiles:
            await upsert_provider_profile(self.db_path, profile)
        return len(profiles)

    async def get_profile_summary(
        self, provider_id: str, model_name: str, task_type: str
    ) -> dict:
        """Returns a simplified key metrics dictionary for consumption by dashboards or loggers."""
        profile = await self.build_provider_profile(provider_id, model_name, task_type)
        return {
            "provider_id": profile.provider_id,
            "model_name": profile.model_name,
            "task_type": profile.task_type.value,
            "total_decisions": profile.total_decisions,
            "success_rate": profile.success_rate,
            "quality_score": profile.quality_score,
            "cold_start": profile.cold_start,
            "profile_confidence": profile.profile_confidence,
        }

    async def _compute_profiles(
        self,
        provider_id: str | None = None,
        model_name: str | None = None,
        task_type: str | None = None,
    ) -> list[ProviderProfile]:
        """Internal helper to aggregate metrics directly using SQL and build profile models."""
        where_parts = []
        params = []
        if provider_id is not None:
            where_parts.append("provider = ?")
            params.append(provider_id)
        if model_name is not None:
            where_parts.append("model_id = ?")
            params.append(model_name)
        if task_type is not None:
            where_parts.append("task_type = ?")
            params.append(task_type)

        where_clause = " AND ".join(where_parts)
        if where_clause:
            where_clause = " AND " + where_clause

        # 1. Distinct combinations of provider, model_id, task_type in history
        combos_query = f"""
            SELECT DISTINCT provider, model_id, task_type FROM ai_decisions WHERE 1=1 {where_clause}
            UNION
            SELECT DISTINCT provider, model_id, task_type FROM ai_mistakes WHERE 1=1 {where_clause}
        """

        # 2. Aggregated decision metrics (using json_extract to read metadata safely)
        decisions_query = f"""
            SELECT 
                provider,
                model_id,
                task_type,
                COUNT(*) as total_decisions,
                SUM(CASE WHEN outcome = 'kept' THEN 1 ELSE 0 END) as successful_decisions,
                SUM(CASE WHEN user_action = 'accepted' THEN 1 ELSE 0 END) as accepted_count,
                SUM(CASE WHEN user_action = 'modified' THEN 1 ELSE 0 END) as modified_count,
                SUM(CASE WHEN user_action = 'rejected' THEN 1 ELSE 0 END) as rejected_count,
                AVG(cast(json_extract(routing_metadata, '$.feedback_score') as REAL)) as avg_feedback_score,
                AVG(cast(json_extract(routing_metadata, '$.latency_ms') as REAL)) as avg_latency_ms,
                AVG(confidence_score) as avg_confidence,
                MAX(timestamp) as last_seen_at
            FROM ai_decisions
            WHERE 1=1 {where_clause}
            GROUP BY provider, model_id, task_type
        """

        # 3. Aggregated mistake metrics (case-insensitive detection signals)
        mistakes_query = f"""
            SELECT 
                provider,
                model_id,
                task_type,
                COUNT(*) as mistake_count,
                SUM(CASE WHEN auto_detected = 0 OR LOWER(detection_signal) IN ('explicit_flag', 'explicit_report') OR failure_type = 'explicit_rejection' THEN 1 ELSE 0 END) as user_reported_mistakes,
                SUM(CASE WHEN correction_made = 1 OR correction_made = True THEN 1 ELSE 0 END) as correction_count,
                SUM(CASE WHEN LOWER(detection_signal) = 'file_revert' THEN 1 ELSE 0 END) as revert_count,
                SUM(CASE WHEN LOWER(detection_signal) = 'heavy_edit' THEN 1 ELSE 0 END) as heavy_edit_count,
                SUM(CASE WHEN LOWER(detection_signal) = 'build_failure' THEN 1 ELSE 0 END) as build_error_count,
                MAX(timestamp) as last_seen_at
            FROM ai_mistakes
            WHERE 1=1 {where_clause}
            GROUP BY provider, model_id, task_type
        """

        # 4. Mistake failure type counts
        failures_by_type_query = f"""
            SELECT 
                provider,
                model_id,
                task_type,
                failure_type,
                COUNT(*) as count
            FROM ai_mistakes
            WHERE 1=1 {where_clause}
            GROUP BY provider, model_id, task_type, failure_type
        """

        combos = []
        decisions_map = {}
        mistakes_map = {}
        failure_types_map = {}

        async with get_db_conn(self.db_path) as db:
            # Query combos
            async with db.execute(combos_query, params * 2) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    combos.append((row["provider"], row["model_id"], row["task_type"]))

            # Query decisions
            async with db.execute(decisions_query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    key = (row["provider"], row["model_id"], row["task_type"])
                    decisions_map[key] = dict(row)

            # Query mistakes
            async with db.execute(mistakes_query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    key = (row["provider"], row["model_id"], row["task_type"])
                    mistakes_map[key] = dict(row)

            # Query failure types
            async with db.execute(failures_by_type_query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    key = (row["provider"], row["model_id"], row["task_type"])
                    if key not in failure_types_map:
                        failure_types_map[key] = {}
                    failure_types_map[key][row["failure_type"]] = row["count"]

        profiles = []
        for provider, model_id, task_type_str in combos:
            try:
                task_type = TaskType(task_type_str)
            except ValueError:
                task_type = TaskType.other

            key = (provider, model_id, task_type_str)
            dec = decisions_map.get(key, {})
            mist = mistakes_map.get(key, {})
            failures = failure_types_map.get(key, {})

            # Base metrics
            total_decisions = dec.get("total_decisions", 0)
            successful_decisions = dec.get("successful_decisions", 0)
            accepted_count = dec.get("accepted_count", 0)
            modified_count = dec.get("modified_count", 0)
            rejected_count = dec.get("rejected_count", 0)
            avg_feedback_score = dec.get("avg_feedback_score")
            avg_latency_ms = dec.get("avg_latency_ms")
            avg_confidence = dec.get("avg_confidence")

            mistake_count = mist.get("mistake_count", 0)
            user_reported_mistakes = mist.get("user_reported_mistakes", 0)
            correction_count = mist.get("correction_count", 0)
            revert_count = mist.get("revert_count", 0)
            heavy_edit_count = mist.get("heavy_edit_count", 0)
            build_error_count = mist.get("build_error_count", 0)

            # Timestamps (last seen is latest of decision or mistake)
            dec_last_seen = dec.get("last_seen_at")
            mist_last_seen = mist.get("last_seen_at")
            last_seen_at = None
            if dec_last_seen and mist_last_seen:
                last_seen_at = max(
                    datetime.fromisoformat(dec_last_seen),
                    datetime.fromisoformat(mist_last_seen),
                )
            elif dec_last_seen:
                last_seen_at = datetime.fromisoformat(dec_last_seen)
            elif mist_last_seen:
                last_seen_at = datetime.fromisoformat(mist_last_seen)

            # Derived Rates (zero-safe)
            success_rate = (
                successful_decisions / total_decisions if total_decisions > 0 else 0.0
            )
            acceptance_rate = (
                accepted_count / total_decisions if total_decisions > 0 else 0.0
            )
            modification_rate = (
                modified_count / total_decisions if total_decisions > 0 else 0.0
            )
            rejection_rate = (
                rejected_count / total_decisions if total_decisions > 0 else 0.0
            )
            revert_rate = revert_count / total_decisions if total_decisions > 0 else 0.0
            mistake_rate = mistake_count / total_decisions if total_decisions > 0 else 0.0
            correction_rate = (
                correction_count / total_decisions if total_decisions > 0 else 0.0
            )
            heavy_edit_rate = (
                heavy_edit_count / total_decisions if total_decisions > 0 else 0.0
            )
            build_error_rate = (
                build_error_count / total_decisions if total_decisions > 0 else 0.0
            )

            # Quality Scoring Calculations
            # Map feedback score to 0..1
            normalized_feedback_score = 0.5  # default baseline
            if avg_feedback_score is not None:
                if 0.0 <= avg_feedback_score <= 1.0:
                    normalized_feedback_score = avg_feedback_score
                elif avg_feedback_score <= 5.0:
                    normalized_feedback_score = avg_feedback_score / 5.0
                else:
                    normalized_feedback_score = min(1.0, avg_feedback_score / 10.0)

            # Latency penalty mapping
            latency_penalty = (
                min(1.0, avg_latency_ms / 5000.0) if avg_latency_ms is not None else 0.0
            )

            # Raw quality score
            raw_quality_score = (
                0.35 * success_rate
                + 0.20 * normalized_feedback_score
                - 0.20 * mistake_rate
                - 0.10 * correction_rate
                - 0.10 * revert_rate
                - 0.05 * latency_penalty
            )
            raw_quality_score = max(0.0, min(1.0, raw_quality_score))

            # Confidence calculations
            confidence_factor = min(1.0, total_decisions / 50.0)

            if total_decisions < 5:
                sample_size_bucket = "tiny"
            elif total_decisions < 10:
                sample_size_bucket = "small"
            elif total_decisions < 50:
                sample_size_bucket = "medium"
            else:
                sample_size_bucket = "large"

            if total_decisions < 10:
                profile_confidence = "low"
            elif total_decisions < 50:
                profile_confidence = "medium"
            else:
                profile_confidence = "high"

            cold_start = total_decisions < 5

            # Blended score with baseline of 0.30
            baseline_score = 0.30
            quality_score = (
                raw_quality_score * confidence_factor
                + baseline_score * (1.0 - confidence_factor)
            )
            quality_score = max(0.0, min(1.0, quality_score))

            profile = ProviderProfile(
                provider=provider,
                model_id=model_id,
                provider_id=provider,
                model_name=model_id,
                task_type=task_type,
                total_decisions=total_decisions,
                successful_decisions=successful_decisions,
                mistake_count=mistake_count,
                user_reported_mistakes=user_reported_mistakes,
                correction_count=correction_count,
                revert_count=revert_count,
                heavy_edit_count=heavy_edit_count,
                build_error_count=build_error_count,
                avg_feedback_score=avg_feedback_score,
                avg_latency_ms=avg_latency_ms,
                success_rate=success_rate,
                acceptance_rate=acceptance_rate,
                modification_rate=modification_rate,
                rejection_rate=rejection_rate,
                revert_rate=revert_rate,
                mistake_rate=mistake_rate,
                correction_rate=correction_rate,
                heavy_edit_rate=heavy_edit_rate,
                build_error_rate=build_error_rate,
                last_seen_at=last_seen_at,
                profile_confidence=profile_confidence,
                confidence_factor=confidence_factor,
                quality_score=quality_score,
                cold_start=cold_start,
                sample_size_bucket=sample_size_bucket,
                mistake_by_type=failures,
                avg_confidence=avg_confidence,
                routing_score=quality_score,
                last_updated=datetime.now(timezone.utc),
            )
            profiles.append(profile)

        return profiles
