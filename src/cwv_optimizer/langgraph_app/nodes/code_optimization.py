"""Node for applying code optimizations via Claude Code."""

from __future__ import annotations

from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing
from cwv_optimizer.services.code_optimizer import apply_code_optimizations

logger = get_logger(__name__)


async def apply_code_optimizations_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that applies code optimizations using Claude Code."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        suggestions_path = current_state.get("parsed_suggestions_path")
        workspace_dir = current_state.get("workspace_dir")

        if not suggestions_path:
            raise RuntimeError("parsed_suggestions_path is required")
        if not workspace_dir:
            raise RuntimeError("workspace_dir is required")

        result = await apply_code_optimizations(
            suggestions_path=suggestions_path,
            workspace_dir=workspace_dir,
            apply_count=current_state.get("apply_count", 1),
            agent=current_state.get("agent", "claude"),
            model=current_state.get("model", "azure/gpt-5"),
            use_architect=current_state.get("use_architect", True),
            branch_logs_dir=current_state.get("branch_logs_dir"),
            results_dir=current_state.get("results_dir"),
        )

        if result.get("status") != "success":
            error = result.get("error", "Code optimization failed")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        current_state["suggestion_results_path"] = result["output_paths"]["application_results_path"]

        logger.info("Code optimizations applied successfully")
        return current_state

    return await run_with_timing("apply_code_optimizations", state, _impl)
