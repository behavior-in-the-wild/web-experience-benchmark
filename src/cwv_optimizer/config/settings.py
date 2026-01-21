"""Application settings using pydantic-settings for robust configuration."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # =========================================================================
    # Project Paths
    # =========================================================================
    project_root: Path = Field(
        default_factory=lambda: Path(__file__).parent.parent.parent.parent,
        description="Root directory of the project",
    )

    @property
    def dumps_dir(self) -> Path:
        """Get the dumps directory, creating it if needed."""
        dumps = self.project_root / "dumps"
        dumps.mkdir(exist_ok=True)
        return dumps

    @property
    def cwv_agent_dir(self) -> Path:
        """Get the CWV agent directory."""
        return self.project_root / "cwv-agent"

    # =========================================================================
    # LLM Configuration
    # =========================================================================
    default_model: str = Field(
        default="azure/gpt-5",
        description="Default LLM model for code changes",
    )
    cwv_model: str = Field(
        default="azure/gpt-5",
        description="LLM model for CWV analysis",
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="LLM temperature",
    )

    # =========================================================================
    # API Keys (loaded from environment)
    # =========================================================================
    openai_api_key: Optional[str] = Field(
        default=None,
        description="OpenAI API key",
    )
    azure_openai_api_key: Optional[str] = Field(
        default=None,
        description="Azure OpenAI API key",
    )
    azure_openai_endpoint: Optional[str] = Field(
        default=None,
        description="Azure OpenAI endpoint",
    )
    anthropic_api_key: Optional[str] = Field(
        default=None,
        description="Anthropic API key (or LiteLLM master key for local proxy)",
    )
    anthropic_base_url: Optional[str] = Field(
        default=None,
        description="Anthropic base URL (e.g., http://localhost:4000 for LiteLLM proxy)",
    )

    # =========================================================================
    # Testing Configuration
    # =========================================================================
    device: Literal["desktop", "mobile"] = Field(
        default="mobile",
        description="Device type for testing",
    )
    headless: bool = Field(
        default=True,
        description="Run browsers headlessly",
    )
    num_runs: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of performance test runs",
    )

    # =========================================================================
    # Optimization Configuration
    # =========================================================================
    agent: Literal["claude"] = Field(
        default="claude",
        description="Coding agent to use (Claude Code)",
    )
    apply_mode: Literal["individual", "batch"] = Field(
        default="individual",
        description="How to apply suggestions",
    )
    apply_count: int = Field(
        default=1,
        ge=1,
        description="Number of times to apply each suggestion",
    )

    # =========================================================================
    # Visual Regression Configuration
    # =========================================================================
    content_similarity_threshold: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Content similarity threshold",
    )
    enable_content_similarity: bool = Field(
        default=True,
        description="Enable content similarity checks",
    )
    run_visual_regression_tests: bool = Field(
        default=True,
        description="Run visual regression tests",
    )

    # =========================================================================
    # S3 Configuration (Optional)
    # =========================================================================
    s3_bucket: Optional[str] = Field(
        default=None,
        description="S3 bucket for archiving",
    )
    s3_prefix: Optional[str] = Field(
        default=None,
        description="S3 prefix for archiving",
    )

    # =========================================================================
    # Logging Configuration
    # =========================================================================
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level",
    )
    log_format: str = Field(
        default="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        description="Log format string",
    )

    # =========================================================================
    # Checkpointing
    # =========================================================================
    checkpoint_db: str = Field(
        default="checkpoints.db",
        description="SQLite database for checkpointing",
    )

    @field_validator("project_root", mode="before")
    @classmethod
    def validate_project_root(cls, v):
        if isinstance(v, str):
            return Path(v)
        return v


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
