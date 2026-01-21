"""Node for running CWV agent analysis on deployed URL."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict

from cwv_optimizer.config import get_settings
from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing

logger = get_logger(__name__)


async def _run_cwv_agent(
    url: str,
    device: str = "mobile",
    model: str = "gpt-5",
    field_url: str = None,
    framework: str = None,
) -> Dict[str, Any]:
    """Run the CWV agent to analyze a deployed URL and generate suggestions.

    Args:
        url: The deployed URL to analyze (can be localhost)
        device: Device type ('mobile' or 'desktop')
        model: LLM model to use for analysis
        field_url: Optional public URL for CrUX/PSI field data collection
        framework: Framework type for context-specific optimization guidance

    Returns:
        Dict containing status, suggestions_path, and report_path
    """
    settings = get_settings()

    try:
        command = [
            "node",
            "index.js",
            "--action", "prompt",
            "--url", url,
            "--model", model,
            "--device", device,
            "--skip-cache"
        ]
        
        # Add --field-url for CrUX/PSI if provided (public URL for field data)
        if field_url:
            command.extend(["--field-url", field_url])
            logger.info("Using field URL for CrUX/PSI: %s", field_url)
        
        # Add --framework for context-specific optimization guidance
        if framework:
            command.extend(["--framework", framework])
            logger.info("Using framework context: %s", framework)

        logger.info("Running CWV agent for URL: %s", url)
        logger.info("Command: %s", " ".join(command))

        # Use Popen for real-time log streaming
        import threading
        
        process = subprocess.Popen(
            command,
            cwd=str(settings.cwv_agent_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        
        stdout_lines = []
        stderr_lines = []
        
        def read_stdout():
            for line in process.stdout:
                line = line.rstrip()
                print(f"[cwv-agent] {line}")  # Real-time output
                stdout_lines.append(line)
        
        def read_stderr():
            for line in process.stderr:
                line = line.rstrip()
                print(f"[cwv-agent:err] {line}")  # Real-time stderr
                stderr_lines.append(line)
        
        # Start threads to read stdout/stderr concurrently
        stdout_thread = threading.Thread(target=read_stdout)
        stderr_thread = threading.Thread(target=read_stderr)
        stdout_thread.start()
        stderr_thread.start()
        
        # Wait for process with timeout
        try:
            process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            process.kill()
            return {"status": "error", "error": "CWV agent timed out after 10 minutes"}
        
        stdout_thread.join()
        stderr_thread.join()
        
        stdout = "\n".join(stdout_lines)
        stderr = "\n".join(stderr_lines)

        if stderr:
            logger.warning("CWV agent stderr: %s", stderr[:500])

        # Parse output for suggestions file path
        suggestions_match = re.search(
            r"✅ Structured suggestions saved at:\s*(.+\.json)",
            stdout,
        )

        report_match = re.search(
            r"✅ CWV report generated at:\s*(.+\.md)",
            stdout,
        )

        if suggestions_match:
            suggestions_path = suggestions_match.group(1).strip()
            report_path = report_match.group(1).strip() if report_match else None

            if Path(suggestions_path).exists():
                return {
                    "status": "success",
                    "suggestions_path": suggestions_path,
                    "report_path": report_path,
                }
            else:
                return {
                    "status": "error",
                    "error": f"Suggestions file not found: {suggestions_path}",
                }
        else:
            # Try alternate pattern
            json_match = re.search(r"(/[^\s]+\.suggestions\.[^\s]+\.json)", stdout)
            if json_match:
                suggestions_path = json_match.group(1).strip()
                if Path(suggestions_path).exists():
                    return {"status": "success", "suggestions_path": suggestions_path}

            return {
                "status": "error",
                "error": "Could not find suggestions file path in output",
            }

    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "CWV agent timed out after 10 minutes"}
    except Exception as e:
        logger.error("Error running CWV agent: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}


async def cwv_analysis_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that runs CWV agent analysis on the deployed URL."""

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        url = current_state.get("deployed_url") or current_state.get("url")
        if not url:
            raise RuntimeError("deployed_url or url is required")

        device = current_state.get("device", "mobile")
        model = current_state.get("cwv_model", "gpt-5")
        
        # Get checked_url for CrUX/PSI field data (from HF dataset)
        field_url = current_state.get("checked_url")
        
        # Get framework for context-specific optimization guidance
        framework = current_state.get("framework")

        result = await _run_cwv_agent(url=url, device=device, model=model, field_url=field_url, framework=framework)

        if result.get("status") != "success":
            error = result.get("error", "CWV analysis failed")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        current_state["parsed_suggestions_path"] = result["suggestions_path"]
        if result.get("report_path"):
            current_state["cwv_report_path"] = result["report_path"]

        logger.info("CWV analysis complete: %s", result["suggestions_path"])
        return current_state

    return await run_with_timing("cwv_analysis", state, _impl)
