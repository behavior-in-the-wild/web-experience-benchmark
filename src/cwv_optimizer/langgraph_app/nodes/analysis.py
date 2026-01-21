"""Node for analyzing performance improvements."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing
from cwv_optimizer.services.reporting import analyze_performance_improvements

logger = get_logger(__name__)


async def analyze_results_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that analyzes performance results."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        testing_results_dir = current_state.get("testing_results_dir")
        if not testing_results_dir:
            raise RuntimeError("testing_results_dir is required")

        result = await asyncio.to_thread(
            analyze_performance_improvements,
            testing_results_dir=testing_results_dir,
            visual_regression_results_path=current_state.get("visual_regression_results_path", ""),
        )

        if result.get("status") != "success":
            error = result.get("error", "Analysis failed")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        current_state["analysis_results_path"] = result["output_paths"]["analysis_results_path"]

        logger.info("Analysis complete: %s", current_state["analysis_results_path"])
        return current_state

    return await run_with_timing("analyze_results", state, _impl)
