"""State schema definitions for the LangGraph optimization workflow."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from typing_extensions import Annotated
from langgraph.graph import add_messages


class OptimizationState(BaseModel):
    """State shared across LangGraph nodes.

    This uses Pydantic for validation and type safety.
    All fields are optional to support partial state updates.
    """

    class Config:
        extra = "allow"  # Allow additional fields for extensibility

    # =========================================================================
    # Pipeline Entry Inputs (from dataset)
    # =========================================================================
    github_url: Optional[str] = Field(
        default=None,
        description="GitHub repository URL to clone",
    )
    revision_id: Optional[str] = Field(
        default=None,
        description="Optional git commit/revision to checkout",
    )
    repo_name: Optional[str] = Field(
        default=None,
        description="Repository name (derived from URL)",
    )

    # =========================================================================
    # Core Configuration
    # =========================================================================
    url: Optional[str] = Field(
        default=None,
        description="Deployed URL for CWV analysis",
    )
    deployed_url: Optional[str] = Field(
        default=None,
        description="URL where the site is deployed",
    )
    checked_url: Optional[str] = Field(
        default=None,
        description="Public live URL for CrUX/PSI field data (from HF dataset)",
    )
    device: str = Field(
        default="mobile",
        description="Device type: 'desktop' or 'mobile'",
    )
    model: str = Field(
        default="azure/gpt-5",
        description="LLM model for code changes",
    )
    cwv_model: str = Field(
        default="azure/gpt-5",
        description="LLM model for CWV analysis",
    )
    parsed_suggestions_path: Optional[str] = Field(
        default=None,
        description="Path to CWV suggestions JSON",
    )
    agent: str = Field(
        default="claude",
        description="Coding agent to use (Claude Code)",
    )
    headless: bool = Field(
        default=True,
        description="Run browsers headlessly",
    )
    num_runs: int = Field(
        default=3,
        description="Number of performance test runs",
    )
    apply_mode: str = Field(
        default="individual",
        description="How to apply suggestions",
    )
    apply_count: int = Field(
        default=1,
        description="Number of times to apply each suggestion",
    )
    temperature: float = Field(
        default=0.0,
        description="LLM temperature",
    )

    # =========================================================================
    # Feature Flags / Thresholds
    # =========================================================================
    content_similarity_threshold: float = Field(
        default=0.9,
        description="Content similarity threshold",
    )
    enable_content_similarity: bool = Field(
        default=True,
        description="Enable content similarity checks",
    )
    run_filter_regression: bool = Field(
        default=True,
        description="Run filter regression",
    )
    run_visual_regression_tests: bool = Field(
        default=True,
        description="Run visual regression tests",
    )

    # =========================================================================
    # Optional Integrations
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
    # Runtime Artifacts (populated by nodes)
    # =========================================================================
    workspace_dir: Optional[str] = Field(
        default=None,
        description="Path to cloned/workspace directory (codebase folder)",
    )
    run_dir: Optional[str] = Field(
        default=None,
        description="Path to the run directory ({repo}_{timestamp})",
    )
    reports_dir: Optional[str] = Field(
        default=None,
        description="Path to the reports directory",
    )
    log_file: Optional[str] = Field(
        default=None,
        description="Path to the run log file",
    )
    server_pid: Optional[int] = Field(
        default=None,
        description="PID of running development server",
    )
    analysis_json_path: Optional[str] = Field(
        default=None,
        description="Path to repository analysis JSON from repo_analyzer node",
    )
    cwv_report_path: Optional[str] = Field(
        default=None,
        description="Path to CWV markdown report",
    )
    suggestion_results_path: Optional[str] = Field(
        default=None,
        description="Path to applied suggestion results",
    )
    visual_regression_results_path: Optional[str] = Field(
        default=None,
        description="Path to visual regression results",
    )
    testing_results_dir: Optional[str] = Field(
        default=None,
        description="Path to testing results directory",
    )
    analysis_results_path: Optional[str] = Field(
        default=None,
        description="Path to analysis results",
    )
    learnings_path: Optional[str] = Field(
        default=None,
        description="Path to learnings file",
    )
    archive_path: Optional[str] = Field(
        default=None,
        description="Path to archive",
    )

    # =========================================================================
    # Observability
    # =========================================================================
    errors: List[str] = Field(
        default_factory=list,
        description="List of errors encountered",
    )
    step_timings: Dict[str, float] = Field(
        default_factory=dict,
        description="Timing for each step",
    )

    # =========================================================================
    # Messages (for agent communication)
    # =========================================================================
    messages: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Conversation messages",
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert state to dictionary for LangGraph compatibility."""
        return self.model_dump(exclude_none=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OptimizationState":
        """Create state from dictionary."""
        return cls(**data)
