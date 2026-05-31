"""Stats Service layer for VexIQ.

Handles aggregated SQL-first queries to compile VexIQ intelligence KPIs,
provider summaries, model breakdowns, and leaderboard ranks.
"""

from datetime import datetime, timezone, timedelta
import json
from vexiq.models import (
    StatsSummaryResponse,
    ProviderKPI,
    ProviderDetailResponse,
    ProviderDetailBreakdown,
    TaskTypeKPI,
    LeaderboardEntry,
)
from vexiq.db import get_db_conn


class StatsService:
    """Service layer that aggregates database records to expose VexIQ analytics and observability."""

    MIN_PROVIDERS_FOR_HIGH_COVERAGE = 3

    def __init__(self, db_path: str):
        """Initializes the StatsService referencing the active SQLite database path."""
        self.db_path = db_path

    async def get_summary(self) -> StatsSummaryResponse:
        """Calculates global metrics and counts for all logged decisions, mistakes, and profiles."""
        decisions_query = """
            SELECT 
                COUNT(*) as total_decisions,
                SUM(CASE WHEN outcome = 'kept' THEN 1 ELSE 0 END) as successful_decisions
            FROM ai_decisions
        """
        profiles_query = """
            SELECT 
                COUNT(DISTINCT provider) as total_providers,
                COUNT(DISTINCT model_id) as total_models,
                COUNT(DISTINCT task_type) as total_task_types,
                AVG(quality_score) as overall_quality_score
            FROM provider_profiles
        """
        mistakes_query = "SELECT COUNT(*) FROM ai_mistakes"
        routing_query = """
            SELECT 
                COUNT(*) as total_calls,
                SUM(CASE WHEN cold_start = 1 THEN 1 ELSE 0 END) as fallback_count
            FROM routing_decisions
        """

        total_decisions = 0
        successful_decisions = 0
        total_providers = 0
        total_models = 0
        total_task_types = 0
        overall_quality_score = 0.0
        total_mistakes = 0
        total_calls = 0
        fallback_count = 0

        async with get_db_conn(self.db_path) as db:
            async with db.execute(decisions_query) as cursor:
                row = await cursor.fetchone()
                if row and row["total_decisions"]:
                    total_decisions = row["total_decisions"]
                    successful_decisions = row["successful_decisions"] or 0

            async with db.execute(profiles_query) as cursor:
                row = await cursor.fetchone()
                if row and row["total_providers"]:
                    total_providers = row["total_providers"]
                    total_models = row["total_models"]
                    total_task_types = row["total_task_types"]
                    overall_quality_score = row["overall_quality_score"] or 0.0

            async with db.execute(mistakes_query) as cursor:
                row = await cursor.fetchone()
                if row:
                    total_mistakes = row[0]

            async with db.execute(routing_query) as cursor:
                row = await cursor.fetchone()
                if row and row["total_calls"]:
                    total_calls = row["total_calls"]
                    fallback_count = row["fallback_count"] or 0

        success_rate = (
            successful_decisions / total_decisions if total_decisions > 0 else 0.0
        )
        fallback_rate = fallback_count / total_calls if total_calls > 0 else 0.0
        # For low confidence routing calls (assume cold start is a proxy or default to 0.0 for now)
        low_confidence_rate = fallback_rate

        return StatsSummaryResponse(
            total_providers=total_providers,
            total_models=total_models,
            total_task_types=total_task_types,
            total_decisions=total_decisions,
            total_mistakes=total_mistakes,
            overall_success_rate=success_rate,
            overall_quality_score=overall_quality_score,
            fallback_rate=fallback_rate,
            low_confidence_rate=low_confidence_rate,
        )

    async def get_providers(
        self,
        task_type: str | None = None,
        min_confidence: float | None = None,
        min_quality_score: float | None = None,
    ) -> list[ProviderKPI]:
        """Calculates provider-level KPIs across task types and models."""
        where_clauses = []
        params = []
        if task_type:
            where_clauses.append("task_type = ?")
            params.append(task_type)
        if min_confidence is not None:
            where_clauses.append("confidence_factor >= ?")
            params.append(min_confidence)
        if min_quality_score is not None:
            where_clauses.append("quality_score >= ?")
            params.append(min_quality_score)

        where_str = ""
        if where_clauses:
            where_str = "WHERE " + " AND ".join(where_clauses)

        query = f"""
            SELECT 
                provider as provider_id,
                SUM(total_decisions) as total_decisions,
                SUM(successful_decisions) as successful_decisions,
                SUM(mistake_count) as mistake_count,
                SUM(correction_count) as correction_count,
                AVG(quality_score) as average_quality_score,
                AVG(confidence_factor) as average_confidence_factor,
                COUNT(DISTINCT task_type) as number_of_task_types_covered,
                SUM(CASE WHEN cold_start = 1 THEN 1 ELSE 0 END) as cold_start_provider_count
            FROM provider_profiles
            {where_str}
            GROUP BY provider
            ORDER BY average_quality_score DESC
        """

        providers = []
        async with get_db_conn(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    total_decisions = row["total_decisions"]
                    success_rate = (
                        row["successful_decisions"] / total_decisions
                        if total_decisions > 0
                        else 0.0
                    )
                    mistake_rate = (
                        row["mistake_count"] / total_decisions
                        if total_decisions > 0
                        else 0.0
                    )
                    correction_rate = (
                        row["correction_count"] / total_decisions
                        if total_decisions > 0
                        else 0.0
                    )

                    providers.append(
                        ProviderKPI(
                            provider_id=row["provider_id"],
                            total_decisions=total_decisions,
                            success_rate=success_rate,
                            mistake_rate=mistake_rate,
                            correction_rate=correction_rate,
                            average_quality_score=row["average_quality_score"] or 0.0,
                            average_confidence_factor=row["average_confidence_factor"]
                            or 0.0,
                            number_of_task_types_covered=row[
                                "number_of_task_types_covered"
                            ],
                            cold_start_provider_count=row[
                                "cold_start_provider_count"
                            ],
                        )
                    )
        return providers

    async def get_provider_detail(self, provider_id: str) -> ProviderDetailResponse:
        """Fetches detailed analytics breakdowns (models, task types, timestamps) for a single provider."""
        # 1. Base provider-level KPIs
        provider_query = """
            SELECT 
                provider as provider_id,
                SUM(total_decisions) as total_decisions,
                SUM(successful_decisions) as successful_decisions,
                SUM(mistake_count) as mistake_count,
                SUM(correction_count) as correction_count,
                AVG(quality_score) as average_quality_score,
                AVG(confidence_factor) as average_confidence_factor,
                COUNT(DISTINCT task_type) as number_of_task_types_covered,
                SUM(CASE WHEN cold_start = 1 THEN 1 ELSE 0 END) as cold_start_provider_count,
                MAX(last_seen_at) as last_seen_str
            FROM provider_profiles
            WHERE provider = ?
            GROUP BY provider
        """

        # 2. Task Type Breakdown
        task_types_query = """
            SELECT 
                task_type as name,
                SUM(total_decisions) as total_decisions,
                SUM(successful_decisions) as successful_decisions,
                SUM(mistake_count) as mistake_count,
                AVG(quality_score) as average_quality_score
            FROM provider_profiles
            WHERE provider = ?
            GROUP BY task_type
        """

        # 3. Model Breakdown
        models_query = """
            SELECT 
                model_id as name,
                SUM(total_decisions) as total_decisions,
                SUM(successful_decisions) as successful_decisions,
                SUM(mistake_count) as mistake_count,
                AVG(quality_score) as average_quality_score
            FROM provider_profiles
            WHERE provider = ?
            GROUP BY model_id
        """

        total_decisions = 0
        success_rate = 0.0
        mistake_rate = 0.0
        correction_rate = 0.0
        average_quality_score = 0.0
        average_confidence_factor = 0.0
        number_of_task_types_covered = 0
        cold_start_provider_count = 0
        last_seen_timestamp = None
        task_type_breakdown = []
        model_breakdown = []

        async with get_db_conn(self.db_path) as db:
            # Execute base provider stats
            async with db.execute(provider_query, (provider_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    total_decisions = row["total_decisions"]
                    success_rate = (
                        row["successful_decisions"] / total_decisions
                        if total_decisions > 0
                        else 0.0
                    )
                    mistake_rate = (
                        row["mistake_count"] / total_decisions
                        if total_decisions > 0
                        else 0.0
                    )
                    correction_rate = (
                        row["correction_count"] / total_decisions
                        if total_decisions > 0
                        else 0.0
                    )
                    average_quality_score = row["average_quality_score"] or 0.0
                    average_confidence_factor = row["average_confidence_factor"] or 0.0
                    number_of_task_types_covered = row["number_of_task_types_covered"]
                    cold_start_provider_count = row["cold_start_provider_count"]
                    
                    last_seen_str = row["last_seen_str"]
                    if last_seen_str:
                        last_seen_timestamp = datetime.fromisoformat(last_seen_str)

            # Execute Task Type breakdowns
            async with db.execute(task_types_query, (provider_id,)) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    dec = row["total_decisions"]
                    task_type_breakdown.append(
                        ProviderDetailBreakdown(
                            name=row["name"],
                            total_decisions=dec,
                            success_rate=row["successful_decisions"] / dec if dec > 0 else 0.0,
                            mistake_rate=row["mistake_count"] / dec if dec > 0 else 0.0,
                            average_quality_score=row["average_quality_score"] or 0.0,
                        )
                    )

            # Execute Model breakdowns
            async with db.execute(models_query, (provider_id,)) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    dec = row["total_decisions"]
                    model_breakdown.append(
                        ProviderDetailBreakdown(
                            name=row["name"],
                            total_decisions=dec,
                            success_rate=row["successful_decisions"] / dec if dec > 0 else 0.0,
                            mistake_rate=row["mistake_count"] / dec if dec > 0 else 0.0,
                            average_quality_score=row["average_quality_score"] or 0.0,
                        )
                    )

        # 4. Recent Decisions (7d, 30d counts)
        now = datetime.now(timezone.utc)
        seven_days_ago = (now - timedelta(days=7)).isoformat()
        thirty_days_ago = (now - timedelta(days=30)).isoformat()

        query_7d = "SELECT COUNT(*) FROM ai_decisions WHERE provider = ? AND timestamp >= ?"
        query_30d = "SELECT COUNT(*) FROM ai_decisions WHERE provider = ? AND timestamp >= ?"

        decisions_last_7_days = 0
        decisions_last_30_days = 0

        async with get_db_conn(self.db_path) as db:
            async with db.execute(query_7d, (provider_id, seven_days_ago)) as cursor:
                row = await cursor.fetchone()
                if row:
                    decisions_last_7_days = row[0]
            async with db.execute(query_30d, (provider_id, thirty_days_ago)) as cursor:
                row = await cursor.fetchone()
                if row:
                    decisions_last_30_days = row[0]

        return ProviderDetailResponse(
            provider_id=provider_id,
            total_decisions=total_decisions,
            success_rate=success_rate,
            mistake_rate=mistake_rate,
            correction_rate=correction_rate,
            average_quality_score=average_quality_score,
            average_confidence_factor=average_confidence_factor,
            number_of_task_types_covered=number_of_task_types_covered,
            cold_start_provider_count=cold_start_provider_count,
            last_seen_timestamp=last_seen_timestamp,
            decisions_last_7_days=decisions_last_7_days,
            decisions_last_30_days=decisions_last_30_days,
            task_type_breakdown=task_type_breakdown,
            model_breakdown=model_breakdown,
        )

    async def get_task_types(self) -> list[TaskTypeKPI]:
        """Calculates per-task-type KPIs including profile coverages, fallback calls, and quality rates."""
        profiles_query = """
            SELECT 
                task_type,
                SUM(total_decisions) as total_decisions,
                SUM(successful_decisions) as successful_decisions,
                SUM(mistake_count) as mistake_count,
                AVG(quality_score) as avg_quality_score,
                COUNT(*) as number_of_profiles,
                SUM(CASE WHEN profile_confidence = 'high' THEN 1 ELSE 0 END) as providers_with_high,
                SUM(CASE WHEN profile_confidence IN ('high', 'medium') THEN 1 ELSE 0 END) as providers_with_med_or_high
            FROM provider_profiles
            GROUP BY task_type
        """

        routing_query = """
            SELECT 
                task_type,
                COUNT(*) as total_calls,
                SUM(CASE WHEN cold_start = 1 THEN 1 ELSE 0 END) as fallback_calls
            FROM routing_decisions
            GROUP BY task_type
        """

        task_types_map = {}

        async with get_db_conn(self.db_path) as db:
            async with db.execute(profiles_query) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    tt = row["task_type"]
                    total_decisions = row["total_decisions"]
                    successful_decisions = row["successful_decisions"]
                    mistakes = row["mistake_count"]
                    avg_quality_score = row["avg_quality_score"] or 0.0
                    number_of_profiles = row["number_of_profiles"]
                    high_providers = row["providers_with_high"] or 0
                    med_high_providers = row["providers_with_med_or_high"] or 0

                    if high_providers >= self.MIN_PROVIDERS_FOR_HIGH_COVERAGE:
                        coverage_quality = "high_coverage"
                    elif med_high_providers > 0:
                        coverage_quality = "medium_coverage"
                    else:
                        coverage_quality = "low_coverage"

                    task_types_map[tt] = {
                        "task_type": tt,
                        "total_decisions": total_decisions,
                        "successful_decisions": successful_decisions,
                        "mistakes": mistakes,
                        "average_quality_score": avg_quality_score,
                        "number_of_profiles": number_of_profiles,
                        "number_of_providers_with_high_confidence": high_providers,
                        "fallback_proportion": 0.0,
                        "coverage_quality": coverage_quality,
                    }

            # Merge routing fallback metrics
            async with db.execute(routing_query) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    tt = row["task_type"]
                    total_calls = row["total_calls"]
                    fallback_calls = row["fallback_calls"] or 0
                    fallback_prop = fallback_calls / total_calls if total_calls > 0 else 0.0

                    if tt in task_types_map:
                        task_types_map[tt]["fallback_proportion"] = fallback_prop
                    else:
                        # Fallback case if routing logs exist but no profile is generated
                        task_types_map[tt] = {
                            "task_type": tt,
                            "total_decisions": 0,
                            "successful_decisions": 0,
                            "mistakes": 0,
                            "average_quality_score": 0.0,
                            "number_of_profiles": 0,
                            "number_of_providers_with_high_confidence": 0,
                            "fallback_proportion": fallback_prop,
                            "coverage_quality": "low_coverage",
                        }

        return [TaskTypeKPI(**v) for v in task_types_map.values()]

    async def get_leaderboard(
        self, task_type: str | None = None, limit: int = 10
    ) -> list[LeaderboardEntry]:
        """Returns provider models ranked by score, breaking ties using confidence and decisions history."""
        where_str = ""
        params = []
        if task_type:
            where_str = "WHERE task_type = ?"
            params.append(task_type)

        query = f"""
            SELECT 
                provider as provider_id,
                model_id as model_name,
                task_type,
                quality_score,
                profile_confidence,
                confidence_factor,
                total_decisions,
                success_rate,
                mistake_rate,
                cold_start
            FROM provider_profiles
            {where_str}
        """

        entries = []
        async with get_db_conn(self.db_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    entries.append(dict(row))

        # Tie breaking ranking key sorting
        def ranking_key(p):
            confidence_rank = (
                0
                if p["profile_confidence"] == "low"
                else (1 if p["profile_confidence"] == "medium" else 2)
            )
            return (
                p["quality_score"] or 0.0,
                confidence_rank,
                p["confidence_factor"] or 0.0,
                0 if p["cold_start"] else 1,
                p["total_decisions"],
            )

        entries.sort(key=ranking_key, reverse=True)

        leaderboard = []
        for i, entry in enumerate(entries[:limit]):
            leaderboard.append(
                LeaderboardEntry(
                    rank=i + 1,
                    provider_id=entry["provider_id"],
                    model_name=entry["model_name"],
                    quality_score=entry["quality_score"] or 0.0,
                    profile_confidence=entry["profile_confidence"],
                    confidence_factor=entry["confidence_factor"] or 0.0,
                    total_decisions=entry["total_decisions"],
                    success_rate=entry["success_rate"] or 0.0,
                    mistake_rate=entry["mistake_rate"] or 0.0,
                    cold_start=bool(entry["cold_start"]),
                )
            )
        return leaderboard
