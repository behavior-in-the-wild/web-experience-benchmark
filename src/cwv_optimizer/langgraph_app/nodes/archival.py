"""Node for archiving results to S3."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing
from cwv_optimizer.services.archival import consolidate_and_archive_results

logger = get_logger(__name__)


async def archive_results_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that archives results to S3."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        s3_bucket = current_state.get("s3_bucket")
        if not s3_bucket:
            logger.info("S3 bucket not configured, skipping archival")
            return current_state

        workspace_dir = current_state.get("workspace_dir")
        if not workspace_dir:
            raise RuntimeError("workspace_dir is required")

        result = await asyncio.to_thread(
            consolidate_and_archive_results,
            dump_dir=str(Path(workspace_dir).parent),
            s3_bucket=s3_bucket,
            s3_prefix=current_state.get("s3_prefix"),
        )

        if result.get("status") != "success":
            error = result.get("error", "Archival failed")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        current_state["archive_path"] = result["output_paths"].get("archive_path")

        logger.info("Results archived to: %s", current_state.get("archive_path"))
        return current_state

    return await run_with_timing("archive_results", state, _impl)
