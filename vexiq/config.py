"""Configuration management for VexIQ.

Defines Pydantic Settings schemas for parsing and validating environment variables 
related to service ports, SQLite database locations, routing thresholds, 
weights, time decay half-lives, and VexCTX connection details.
"""

import os
from functools import lru_cache
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """VexIQ Settings schema loading from environment variables or .env."""

    vexiq_port: int = 8767
    vexiq_db_path: str = "~/.vexiq/vexiq.db"
    vexiq_log_level: str = "INFO"
    vexiq_min_decisions_before_scoring: int = 5
    vexiq_revert_window_minutes: int = 60
    vexiq_build_failure_window_minutes: int = 5
    vexiq_heavy_edit_threshold: float = 0.5
    vexiq_default_provider_priority: str = "claude,gpt-4o,ollama/llama3.2"
    vexctx_base_url: str = "http://localhost:8765"
    vexiq_vexctx_sync_enabled: bool = True
    vexiq_weight_revert: float = 2.0
    vexiq_weight_mistake: float = 1.5
    vexiq_weight_modification: float = 0.5
    vexiq_weight_confidence: float = 0.3
    vexiq_score_decay_halflife_days: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("vexiq_db_path", mode="after")
    @classmethod
    def expand_db_path(cls, v: str) -> str:
        """Expands user tilde (~) and converts to an absolute path."""
        return os.path.abspath(os.path.expanduser(v))

    @field_validator("vexiq_heavy_edit_threshold")
    @classmethod
    def validate_heavy_edit_threshold(cls, v: float) -> float:
        """Validates that the heavy edit threshold lies between 0.0 and 1.0 inclusive."""
        if not (0.0 <= v <= 1.0):
            raise ValueError("vexiq_heavy_edit_threshold must be between 0.0 and 1.0")
        return v

    @field_validator(
        "vexiq_weight_revert",
        "vexiq_weight_mistake",
        "vexiq_weight_modification",
        "vexiq_weight_confidence",
    )
    @classmethod
    def validate_non_negative_weights(cls, v: float) -> float:
        """Validates that scoring weights are non-negative numbers."""
        if v < 0.0:
            raise ValueError("Numeric weights must be non-negative")
        return v

    @property
    def provider_priority_list(self) -> list[str]:
        """Returns default provider priority values as a list of trimmed strings."""
        return [
            s.strip()
            for s in self.vexiq_default_provider_priority.split(",")
            if s.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    """Returns cached Settings instance."""
    return Settings()
