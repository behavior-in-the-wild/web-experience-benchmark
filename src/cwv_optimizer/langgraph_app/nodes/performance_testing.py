"""Node for running CWV performance tests."""

from __future__ import annotations

from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing
from cwv_optimizer.services.performance_testing import run_cwv_tests

logger = get_logger(__name__)


async def run_performance_testing_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that runs CWV performance tests."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        suggestion_results_path = current_state.get("suggestion_results_path")
        if not suggestion_results_path:
            raise RuntimeError("suggestion_results_path is required")

        result = await run_cwv_tests(
            workspace_dir=current_state["workspace_dir"],
            application_results_path=suggestion_results_path,
            visual_regression_results_path=current_state.get("visual_regression_results_path", ""),
            url=current_state["url"],
            device=current_state["device"],
            num_runs=current_state.get("num_runs", 3),
            headless=current_state.get("headless", True),
            run_visual_regression_tests=current_state.get("run_visual_regression_tests", True),
            framework=current_state.get("framework", "Static HTML"),
            results_dir=current_state.get("results_dir"),
        )

        if result.get("status") != "success":
            error = result.get("error", "Performance testing failed")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        current_state["testing_results_dir"] = result["output_paths"]["testing_results_directory"]

        logger.info("Performance testing complete")
        return current_state

    return await run_with_timing("run_performance_testing", state, _impl)
