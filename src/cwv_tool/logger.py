"""Logging utilities for cwv_tool."""

from __future__ import annotations

import logging
import sys
from typing import Optional

LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(getattr(logging, LOG_LEVEL))
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(getattr(logging, LOG_LEVEL))
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def setup_logging(level: Optional[str] = None, format_string: Optional[str] = None) -> None:
    logging.basicConfig(
        level=getattr(logging, level or LOG_LEVEL),
        format=format_string or LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    for noisy in ("httpx", "httpcore", "urllib3", "LiteLLM", "git", "asyncio", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
