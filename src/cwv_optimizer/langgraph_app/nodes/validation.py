"""Validation nodes for input verification."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing

logger = get_logger(__name__)


def _validate_github_url(url: str) -> bool:
    """Validate that a string is a valid GitHub URL."""
    pattern = r"^https?://github\.com/[\w\-\.]+/[\w\-\.]+/?$"
    return bool(re.match(pattern, url))


def _validate_full_pipeline_config(config: Dict[str, Any]) -> None:
    """Validate configuration for the full pipeline."""
    if not config.get("github_url"):
        raise ValueError("github_url is required for full pipeline")

    if not _validate_github_url(config["github_url"]):
        raise ValueError(f"Invalid GitHub URL: {config['github_url']}")

    if config.get("device") and config["device"] not in ["desktop", "mobile"]:
        raise ValueError(f"Invalid device type: {config['device']}")


def _validate_optimization_config(config: Dict[str, Any]) -> None:
    """Validate configuration for optimization-only pipeline."""
    required_fields = ["url", "parsed_suggestions_path", "workspace_dir"]

    for field in required_fields:
        if not config.get(field):
            raise ValueError(f"Required field '{field}' is missing")

    # Validate parsed suggestions file exists
    suggestions_path = Path(config["parsed_suggestions_path"])
    if not suggestions_path.exists():
        raise FileNotFoundError(
            f"Parsed suggestions file not found: {suggestions_path}"
        )

    # Validate workspace directory exists
    workspace_path = Path(config["workspace_dir"])
    if not workspace_path.exists():
        raise FileNotFoundError(f"Workspace directory not found: {workspace_path}")

    if config.get("device") and config["device"] not in ["desktop", "mobile"]:
        raise ValueError(f"Invalid device type: {config['device']}")


async def validate_input_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Validate input for optimization-only pipeline."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        _validate_optimization_config(current_state)
        logger.info("Input validation passed")
        return current_state

    return await run_with_timing("validate_input", state, _impl)


async def validate_full_pipeline_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Validate input for full pipeline (from GitHub URL)."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        _validate_full_pipeline_config(current_state)
        logger.info("Full pipeline validation passed for: %s", current_state.get("github_url"))
        return current_state

    return await run_with_timing("validate_full_pipeline", state, _impl)


# ==============================
# Framework Pipeline Validation
# ==============================

VALID_FRAMEWORKS = {
    "Hexo", "Jekyll", "Static HTML", "Static Html",
    "Hugo", "Vue", "React", "Next", "Flask", "Pelican", "Express", "Quarto"
}

# Map for normalizing framework names (case-insensitive)
FRAMEWORK_NORMALIZE = {
    "static html": "Static HTML",
    "static htm": "Static HTML",
    "hexo": "Hexo",
    "jekyll": "Jekyll",
    "hugo": "Hugo",
    "vue": "Vue",
    "vue.js": "Vue",
    "react": "React",
    "next": "Next",
    "next.js": "Next",
    "nextjs": "Next",
    "flask": "Flask",
    "pelican": "Pelican",
    "express": "Express",
    "quarto": "Quarto",
}


def _validate_framework_pipeline_config(config: Dict[str, Any]) -> None:
    """Validate configuration for framework-based pipeline."""
    if not config.get("github_url"):
        raise ValueError("github_url is required for framework pipeline")
    
    if not _validate_github_url(config["github_url"]):
        raise ValueError(f"Invalid GitHub URL: {config['github_url']}")
    
    if not config.get("framework"):
        raise ValueError("framework is required for framework pipeline")
    
    framework = config["framework"]
    framework_lower = framework.lower()
    
    # Normalize framework name
    if framework_lower in FRAMEWORK_NORMALIZE:
        config["framework"] = FRAMEWORK_NORMALIZE[framework_lower]
        return
    
    # Check if already valid
    if framework in VALID_FRAMEWORKS:
        return
        
    raise ValueError(f"Invalid framework: {framework}. Must be one of: {VALID_FRAMEWORKS}")


async def validate_framework_pipeline_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Validate input for framework pipeline (from JSONL with framework)."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        _validate_framework_pipeline_config(current_state)
        logger.info(
            "Framework pipeline validation passed: %s (framework: %s)",
            current_state.get("github_url"),
            current_state.get("framework"),
        )
        return current_state

    return await run_with_timing("validate_framework_pipeline", state, _impl)
