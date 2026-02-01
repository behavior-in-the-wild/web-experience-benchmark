"""Post-processing node for patch generation and S3 archival."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing
from cwv_optimizer.services.archival import consolidate_and_archive_results, generate_patches

logger = get_logger(__name__)


async def post_processing_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that generates patches and archives results to S3."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        workspace_dir = current_state.get("workspace_dir")
        if not workspace_dir:
            raise RuntimeError("workspace_dir is required")

        dump_dir = str(Path(workspace_dir).parent)
        codebase_dir = workspace_dir

        # Step 1: Generate patches for each optimization branch
        logger.info("Generating patches for optimization branches...")
        patch_result = await asyncio.to_thread(
            generate_patches,
            codebase_dir=codebase_dir,
            output_dir=dump_dir,
        )

        if patch_result.get("status") == "success":
            patches = patch_result.get("patches", [])
            current_state["patches"] = patches
            logger.info("Generated %d patches", len(patches))
        else:
            logger.warning("Patch generation failed: %s", patch_result.get("error"))

        # Step 2: Archive to S3 if configured
        s3_bucket = current_state.get("s3_bucket")
        if s3_bucket:
            logger.info("Archiving results to S3...")
            archive_result = await asyncio.to_thread(
                consolidate_and_archive_results,
                dump_dir=dump_dir,
                s3_bucket=s3_bucket,
                s3_prefix=current_state.get("s3_prefix"),
            )

            if archive_result.get("status") != "success":
                error = archive_result.get("error", "Archival failed")
                current_state.setdefault("errors", []).append(error)
                logger.error("S3 archival failed: %s", error)
            else:
                current_state["archive_path"] = archive_result["output_paths"].get("archive_path")
                logger.info("Results archived to: %s", current_state.get("archive_path"))
        else:
            logger.info("S3 bucket not configured, skipping S3 archival")

        return current_state

    return await run_with_timing("post_processing", state, _impl)
