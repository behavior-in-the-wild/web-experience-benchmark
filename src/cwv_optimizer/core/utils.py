"""Generic utility functions for CWV Optimizer."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from cwv_optimizer.config import get_settings
from cwv_optimizer.core.logger import get_logger

logger = get_logger(__name__)


def format_model_name(model: str, for_litellm: bool = False) -> str:
    """Format model name for different contexts.

    Args:
        model: Model name (e.g., 'gpt-4.1', 'azure/gpt-4o')
        for_litellm: If True, format for litellm/Azure usage

    Returns:
        Formatted model name
    """
    if for_litellm:
        # For litellm, use azure/ prefix for Azure models
        if model.startswith("azure/"):
            return model
        # Map common model names to Azure format
        model_mapping = {
            "gpt-4.1": "azure/gpt-4.1",
            "gpt-4o": "azure/gpt-4o",
            "gpt-5": "azure/gpt-5",
        }
        return model_mapping.get(model, f"azure/{model}")

    return model


def save_json_file(data: Dict[str, Any], filepath: Path) -> None:
    """Save data to a JSON file.

    Args:
        data: Data to save
        filepath: Path to save to
    """
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    logger.debug("Saved JSON file: %s", filepath)


def get_session_dir(workspace_dir: str, prefix: str = "session") -> Path:
    """Create a timestamped session directory.

    Args:
        workspace_dir: Base workspace directory
        prefix: Directory prefix

    Returns:
        Path to created session directory
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = Path(workspace_dir) / f"{prefix}_{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def sanitize_branch_name(name: str) -> str:
    """Sanitize a string to be a valid git branch name.

    Args:
        name: Raw branch name

    Returns:
        Sanitized branch name
    """
    # Replace invalid characters with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    # Remove consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized)
    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")
    return sanitized[:50]  # Limit length


def parse_github_url(url: str) -> Dict[str, str]:
    """Parse a GitHub URL into components.

    Args:
        url: GitHub repository URL

    Returns:
        Dict with owner, repo, and optional path
    """
    # Remove trailing slashes and .git
    url = url.rstrip("/").replace(".git", "")

    # Match GitHub URL pattern
    pattern = r"https?://github\.com/([^/]+)/([^/]+)(?:/(.+))?"
    match = re.match(pattern, url)

    if not match:
        raise ValueError(f"Invalid GitHub URL: {url}")

    return {
        "owner": match.group(1),
        "repo": match.group(2),
        "path": match.group(3) or "",
    }
