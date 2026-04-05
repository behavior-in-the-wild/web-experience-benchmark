"""Centralized logging configuration for CWV Optimizer."""

from __future__ import annotations

import logging
import sys
from typing import Optional

from cwv_optimizer.config import get_settings


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with consistent formatting.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    if not logger.handlers:
        settings = get_settings()
        logger.setLevel(getattr(logging, settings.log_level))

        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(getattr(logging, settings.log_level))

        formatter = logging.Formatter(settings.log_format)
        handler.setFormatter(formatter)

        logger.addHandler(handler)
        logger.propagate = False

    return logger


def setup_logging(
    level: Optional[str] = None,
    format_string: Optional[str] = None,
) -> None:
    """Configure root logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        format_string: Custom format string
    """
    settings = get_settings()

    log_level = level or settings.log_level
    log_format = format_string or settings.log_format

    logging.basicConfig(
        level=getattr(logging, log_level),
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("git").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
