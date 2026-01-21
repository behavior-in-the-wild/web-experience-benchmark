"""Reporting service for analysis and learnings."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.core.utils import save_json_file

logger = get_logger(__name__)


def analyze_performance_improvements(
    testing_results_dir: str,
    visual_regression_results_path: str = "",
) -> Dict[str, Any]:
    """Analyze performance test results and generate insights.

    Args:
        testing_results_dir: Path to testing results directory
        visual_regression_results_path: Optional path to regression results

    Returns:
        Result dictionary with analysis path
    """
    logger.info("Analyzing performance results from: %s", testing_results_dir)

    try:
        results_dir = Path(testing_results_dir)
        summary_path = results_dir / "cwv_summary.json"

        if not summary_path.exists():
            return {"status": "error", "error": "CWV summary not found"}

        with open(summary_path) as f:
            cwv_data = json.load(f)

        results = cwv_data.get("results", [])

        # Handle case where no branches were tested
        if cwv_data.get("message") == "No branches to test" or not results:
            logger.info("No branches were tested, creating empty analysis")
            analysis = {
                "timestamp": datetime.now().isoformat(),
                "baseline": None,
                "improvements": [],
                "regressions": [],
                "summary": {
                    "total_tested": 0,
                    "improved": 0,
                    "regressed": 0,
                    "no_change": 0,
                    "message": "No branches to test",
                },
            }
            analysis_path = results_dir / "analysis_results.json"
            save_json_file(analysis, analysis_path)
            return {
                "status": "success",
                "output_paths": {"analysis_results_path": str(analysis_path)},
                "summary": analysis["summary"],
            }

        # Find baseline
        baseline = next((r for r in results if r.get("is_baseline")), None)
        if not baseline:
            return {"status": "error", "error": "Baseline results not found"}

        # Analyze improvements
        analysis = {
            "timestamp": datetime.now().isoformat(),
            "baseline": baseline,
            "improvements": [],
            "regressions": [],
            "summary": {
                "total_tested": len(results) - 1,
                "improved": 0,
                "regressed": 0,
                "no_change": 0,
            },
        }

        for result in results:
            if result.get("is_baseline"):
                continue

            # Calculate actual improvement based on LCP median
            baseline_lcp = baseline.get("metrics", {}).get("LCP_median", 0)
            branch_lcp = result.get("metrics", {}).get("LCP_median", 0)
            
            if baseline_lcp > 0:
                # Positive = improvement (lower LCP is better)
                # Negative = regression (higher LCP is worse)
                improvement_pct = round(
                    (baseline_lcp - branch_lcp) / baseline_lcp * 100, 2
                )
            else:
                improvement_pct = 0
            
            comparison = {
                "branch": result.get("branch"),
                "metrics": result.get("metrics", {}),
                "baseline_lcp": baseline_lcp,
                "branch_lcp": branch_lcp,
                "improvement_percent": improvement_pct,
            }

            # Categorize based on improvement threshold (5% to account for variance)
            if improvement_pct > 5:
                analysis["improvements"].append(comparison)
                analysis["summary"]["improved"] += 1
            elif improvement_pct < -5:
                analysis["regressions"].append(comparison)
                analysis["summary"]["regressed"] += 1
            else:
                analysis["summary"]["no_change"] += 1

        # Save analysis
        analysis_path = results_dir / "analysis_results.json"
        save_json_file(analysis, analysis_path)

        return {
            "status": "success",
            "output_paths": {
                "analysis_results_path": str(analysis_path),
            },
            "summary": analysis["summary"],
        }

    except Exception as e:
        logger.error("Analysis failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}


def generate_learnings(
    workspace_dir: str,
    analysis_results_path: str,
    suggestion_results_path: str,
    visual_regression_results_path: str = "",
    url: str = "",
    device: str = "mobile",
    model: str = "azure/gpt-5",
    apply_mode: str = "individual",
) -> Dict[str, Any]:
    """Generate learnings from the optimization results.

    Args:
        workspace_dir: Path to workspace directory
        analysis_results_path: Path to analysis results
        suggestion_results_path: Path to suggestion results
        visual_regression_results_path: Optional path to regression results
        url: Target URL
        device: Device type
        model: Model used
        apply_mode: Application mode

    Returns:
        Result dictionary with learnings path
    """
    logger.info("Generating learnings from: %s", analysis_results_path)

    try:
        workspace_path = Path(workspace_dir)
        dump_dir = workspace_path.parent

        # Load analysis results
        with open(analysis_results_path) as f:
            analysis_data = json.load(f)

        # Load suggestion results
        with open(suggestion_results_path) as f:
            suggestion_data = json.load(f)

        # Generate learnings
        learnings = {
            "timestamp": datetime.now().isoformat(),
            "url": url,
            "device": device,
            "model": model,
            "apply_mode": apply_mode,
            "summary": {
                "total_suggestions": len(suggestion_data.get("results", [])),
                "successful_applications": sum(
                    1 for r in suggestion_data.get("results", [])
                    if r.get("status") == "success"
                ),
                "improvements_found": len(analysis_data.get("improvements", [])),
            },
            "key_learnings": [],
            "recommendations": [],
            "analysis_summary": analysis_data.get("summary", {}),
        }

        # TODO: Use LLM to generate insights
        # This is a placeholder
        if analysis_data.get("improvements"):
            learnings["key_learnings"].append(
                "Performance optimizations were successfully applied and measured."
            )
            learnings["recommendations"].append(
                "Review the top-performing optimizations for production deployment."
            )

        # Save learnings
        learnings_path = dump_dir / f"learnings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        save_json_file(learnings, learnings_path)

        return {
            "status": "success",
            "output_paths": {
                "learnings_path": str(learnings_path),
                "analysis_results_path": analysis_results_path,
            },
            "summary": learnings["summary"],
        }

    except Exception as e:
        logger.error("Learnings generation failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}
