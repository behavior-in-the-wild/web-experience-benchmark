"""Base utilities for LangGraph nodes."""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable, Dict, Any

from cwv_optimizer.core.logger import get_logger

logger = get_logger(__name__)


async def run_with_timing(
    node_name: str,
    state: Dict[str, Any],
    func: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Execute a node function with timing and logging.

    Args:
        node_name: Name of the node for logging
        state: Current state dictionary
        func: Async function to execute

    Returns:
        Updated state dictionary
    """
    start = datetime.now()
    logger.info("Starting node: %s", node_name)

    try:
        new_state = await func(state)
        return new_state
    except Exception as e:
        logger.error("Node %s failed: %s", node_name, e, exc_info=True)
        raise
    finally:
        duration = (datetime.now() - start).total_seconds()
        step_timings = state.get("step_timings", {})
        step_timings[node_name] = step_timings.get(node_name, 0.0) + duration
        state["step_timings"] = step_timings
        logger.info("Completed node: %s in %.2fs", node_name, duration)
