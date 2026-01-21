"""Node for generating learnings reports."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing
from cwv_optimizer.services.reporting import generate_learnings

logger = get_logger(__name__)


async def generate_learnings_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that generates learnings from results."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        analysis_results_path = current_state.get("analysis_results_path")
        if not analysis_results_path:
            raise RuntimeError("analysis_results_path is required")

        result = await asyncio.to_thread(
            generate_learnings,
            workspace_dir=current_state["workspace_dir"],
            analysis_results_path=analysis_results_path,
            suggestion_results_path=current_state["suggestion_results_path"],
            visual_regression_results_path=current_state.get("visual_regression_results_path", ""),
            url=current_state["url"],
            device=current_state["device"],
            model=current_state["model"],
            apply_mode=current_state.get("apply_mode", "individual"),
        )

        if result.get("status") != "success":
            error = result.get("error", "Learnings generation failed")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        current_state["learnings_path"] = result["output_paths"]["learnings_path"]

        logger.info("Learnings generated: %s", current_state["learnings_path"])
        return current_state

    return await run_with_timing("generate_learnings", state, _impl)
