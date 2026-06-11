"""Utility functions for cwv_tool."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from cwv_tool.logger import get_logger

logger = get_logger(__name__)


def save_json_file(data: Dict[str, Any], filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    logger.debug("Saved JSON file: %s", filepath)


def get_session_dir(workspace_dir: str, prefix: str = "session") -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = Path(workspace_dir) / f"{prefix}_{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def sanitize_branch_name(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized)
    return sanitized.strip("_")[:50]


def parse_github_url(url: str) -> Dict[str, str]:
    url = url.rstrip("/").replace(".git", "")
    pattern = r"https?://github\.com/([^/]+)/([^/]+)(?:/(.+))?"
    match = re.match(pattern, url)
    if not match:
        raise ValueError(f"Invalid GitHub URL: {url}")
    return {"owner": match.group(1), "repo": match.group(2), "path": match.group(3) or ""}
