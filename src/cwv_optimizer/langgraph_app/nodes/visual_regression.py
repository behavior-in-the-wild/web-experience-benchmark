"""Node for visual regression testing."""

from __future__ import annotations

from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing
from cwv_optimizer.services.visual_regression import run_visual_regression_tests

logger = get_logger(__name__)


async def visual_regression_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that runs visual regression tests."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        if not current_state.get("run_visual_regression_tests", True):
            logger.info("Visual regression tests disabled, skipping")
            current_state["visual_regression_results_path"] = ""
            return current_state

        result = await run_visual_regression_tests(
            workspace_dir=current_state["workspace_dir"],
            url=current_state["url"],
            device=current_state["device"],
            headless=current_state.get("headless", True),
            content_similarity_threshold=current_state.get("content_similarity_threshold", 0.9),
            enable_content_similarity=current_state.get("enable_content_similarity", True),
            apply_mode=current_state.get("apply_mode", "individual"),
            model=current_state["model"],
            framework=current_state.get("framework", "Static HTML"),
            server_pid=current_state.get("server_pid"),
            results_dir=current_state.get("results_dir"),
            screenshots_dir=current_state.get("screenshots_dir"),
        )

        if result.get("status") == "error":
            error = result.get("error", "Visual regression failed")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        current_state["visual_regression_results_path"] = result.get("regression_report_path", "")

        logger.info("Visual regression tests complete")
        return current_state

    return await run_with_timing("visual_regression", state, _impl)
