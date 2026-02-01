"""Archival service for storing results to S3."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from cwv_optimizer.core.logger import get_logger

logger = get_logger(__name__)


def consolidate_and_archive_results(
    dump_dir: str,
    s3_bucket: Optional[str] = None,
    s3_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """Consolidate results and archive to S3.

    Args:
        dump_dir: Path to dump directory
        s3_bucket: S3 bucket name
        s3_prefix: S3 prefix

    Returns:
        Result dictionary with archive path
    """
    logger.info("Consolidating results from: %s", dump_dir)

    try:
        dump_path = Path(dump_dir)

        # Create archive directory
        archive_name = f"cwv_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        archive_dir = dump_path / archive_name
        archive_dir.mkdir(exist_ok=True)

        # Collect all result files
        result_files = list(dump_path.glob("*.json"))
        result_files.extend(dump_path.glob("**/cwv_summary.json"))
        result_files.extend(dump_path.glob("**/analysis_results.json"))

        for result_file in result_files:
            if result_file.is_file():
                shutil.copy(result_file, archive_dir / result_file.name)

        # Create archive metadata
        metadata = {
            "created_at": datetime.now().isoformat(),
            "files": [f.name for f in archive_dir.iterdir()],
            "source_dir": dump_dir,
        }

        with open(archive_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        # Create zip archive
        archive_path = shutil.make_archive(
            str(dump_path / archive_name),
            "zip",
            archive_dir,
        )

        # Upload to S3 if configured
        if s3_bucket:
            try:
                import boto3

                s3 = boto3.client("s3")
                s3_key = f"{s3_prefix}/{archive_name}.zip" if s3_prefix else f"{archive_name}.zip"

                s3.upload_file(archive_path, s3_bucket, s3_key)
                logger.info("Uploaded archive to s3://%s/%s", s3_bucket, s3_key)

            except ImportError:
                logger.warning("boto3 not installed, skipping S3 upload")
            except Exception as e:
                logger.error("S3 upload failed: %s", e)

        # Cleanup temp archive directory
        shutil.rmtree(archive_dir)

        return {
            "status": "success",
            "output_paths": {
                "archive_path": archive_path,
            },
            "summary": {
                "files_archived": len(result_files),
            },
        }

    except Exception as e:
        logger.error("Archival failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}


def generate_patches(codebase_dir: str, output_dir: str) -> Dict[str, Any]:
    """Generate git patches for each optimization branch.

    Creates patches/ folder with .patch files for each branch vs baseline.

    Args:
        codebase_dir: Path to git repository (codebase folder)
        output_dir: Path to dump directory where patches/ folder will be created

    Returns:
        Result dictionary with list of generated patch files
    """
    import subprocess

    logger.info("Generating patches from: %s", codebase_dir)

    try:
        patches_dir = Path(output_dir) / "patches"
        patches_dir.mkdir(exist_ok=True)

        # Get all local branches
        result = subprocess.run(
            ["git", "branch", "--list"],
            cwd=codebase_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            logger.warning("Failed to list branches: %s", result.stderr)
            return {"status": "error", "error": result.stderr}

        branches = [b.strip().lstrip("* ") for b in result.stdout.splitlines() if b.strip()]
        logger.info("Found branches: %s", branches)

        # Filter to optimization branches (exclude main/master/baseline)
        excluded = {"main", "master", "baseline", "HEAD"}
        opt_branches = [b for b in branches if b not in excluded]

        if not opt_branches:
            logger.info("No optimization branches found to generate patches for")
            return {"status": "success", "patches": []}

        # Check if baseline exists
        baseline_branch = "baseline" if "baseline" in branches else "main" if "main" in branches else "master"

        patches = []
        for branch in opt_branches:
            patch_file = patches_dir / f"{branch}.patch"

            # Generate diff excluding .aider files and hidden directories
            diff_result = subprocess.run(
                ["git", "diff", baseline_branch, branch, "--", ".", ":(exclude).aider*", ":(exclude).git*"],
                cwd=codebase_dir,
                capture_output=True,
                text=True,
            )

            if diff_result.returncode == 0 and diff_result.stdout.strip():
                patch_file.write_text(diff_result.stdout)
                patches.append(str(patch_file))
                logger.info("Generated patch: %s", patch_file.name)
            else:
                logger.debug("No diff for branch %s vs %s", branch, baseline_branch)

        logger.info("Generated %d patches in %s", len(patches), patches_dir)

        return {
            "status": "success",
            "patches": patches,
            "patches_dir": str(patches_dir),
        }

    except Exception as e:
        logger.error("Patch generation failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}
