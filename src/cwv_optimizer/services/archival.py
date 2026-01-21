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
