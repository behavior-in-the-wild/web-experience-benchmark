"""Node for running CWV agent analysis on deployed URL."""

from __future__ import annotations

import re
import shutil
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
    workspace_dir: str = None,
) -> Dict[str, Any]:
    """Run the CWV agent to analyze a deployed URL and generate suggestions.

    Args:
        url: The deployed URL to analyze (can be localhost)
        device: Device type ('mobile' or 'desktop')
        model: LLM model to use for analysis
        field_url: Optional public URL for CrUX/PSI field data collection
        framework: Framework type for context-specific optimization guidance
        workspace_dir: Directory where prompts.log should be saved

    Returns:
        Dict containing status, suggestions_path, and report_path
    """
    settings = get_settings()

    # Browser crashes (Protocol error / detached Frame) are transient — retry once.
    _BROWSER_CRASH_PATTERNS = (
        "Protocol error",
        "Target closed",
        "detached Frame",
        "Session closed",
        "Execution context was destroyed",
    )
    MAX_ATTEMPTS = 2

    try:
        command = [
            "node",
            "index.js",
            "--action", "agent",
            "--url", url,
            "--model", model,
            "--device", device,
            "--skip-cache",
            "--log-path", str(Path(workspace_dir) / "prompts.log") if workspace_dir else ".cache/prompts.log"
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

        import threading
        import time as _time

        stdout = ""
        stderr = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt > 1:
                logger.warning("CWV agent browser crash detected — retrying (attempt %d/%d)", attempt, MAX_ATTEMPTS)
                _time.sleep(5)

            process = subprocess.Popen(
                command,
                cwd=str(settings.cwv_agent_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def read_stdout():
                for line in process.stdout:
                    line = line.rstrip()
                    print(f"[cwv-agent] {line}")
                    stdout_lines.append(line)

            def read_stderr():
                for line in process.stderr:
                    line = line.rstrip()
                    print(f"[cwv-agent:err] {line}")
                    stderr_lines.append(line)

            stdout_thread = threading.Thread(target=read_stdout)
            stderr_thread = threading.Thread(target=read_stderr)
            stdout_thread.start()
            stderr_thread.start()

            try:
                process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                process.kill()
                return {"status": "error", "error": "CWV agent timed out after 10 minutes"}

            stdout_thread.join()
            stderr_thread.join()

            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)

            # If no browser crash, stop retrying
            if not any(pat in stderr for pat in _BROWSER_CRASH_PATTERNS):
                break

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
            # Try alternate pattern (absolute path in stdout)
            json_match = re.search(r"(/[^\s]+\.suggestions\.[^\s]+\.json)", stdout)
            if json_match:
                suggestions_path = json_match.group(1).strip()
                if Path(suggestions_path).exists():
                    return {"status": "success", "suggestions_path": suggestions_path}

            # If the site performed well, the agent may have written the report but
            # skipped suggestions (old cwv-agent). Synthesise a minimal JSON so the
            # pipeline can still record a result.
            if "Site Performs Well" in stdout or "site performs well" in stdout.lower():
                import json as _json
                import tempfile

                report_path_val = report_match.group(1).strip() if report_match else None
                minimal = {
                    "url": url,
                    "deviceType": device,
                    "suggestions": [],
                    "summary": {
                        "earlyExit": True,
                        "reason": "All Core Web Vitals pass good thresholds",
                    },
                }
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".suggestions.json", delete=False
                )
                _json.dump(minimal, tmp)
                tmp.close()
                return {
                    "status": "success",
                    "suggestions_path": tmp.name,
                    "report_path": report_path_val,
                }

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
        model = current_state.get("cwv_model", "gpt-4.1")
        # cwv-agent expects bare model/deployment names like "gpt-4.1"
        # (not provider-prefixed strings like "azure/gpt-4.1").
        if isinstance(model, str) and "/" in model:
            model = model.split("/")[-1]
        
        # Get checked_url for CrUX/PSI field data (from HF dataset)
        field_url = current_state.get("checked_url")
        
        # Get framework for context-specific optimization guidance
        framework = current_state.get("framework")

        result = await _run_cwv_agent(url=url, device=device, model=model, field_url=field_url, framework=framework, workspace_dir=current_state.get("workspace_dir"))

        if result.get("status") != "success":
            error = result.get("error", "CWV analysis failed")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        # Persist suggestions (and report) into the run's results directory so later
        # pipeline stages and external harnesses can reliably find them.
        results_dir = current_state.get("results_dir")
        if results_dir:
            results_path = Path(results_dir)
        else:
            workspace_dir = current_state.get("workspace_dir")
            results_path = (Path(workspace_dir).parent / "results") if workspace_dir else None

        suggestions_src = Path(result["suggestions_path"])
        if results_path:
            results_path.mkdir(parents=True, exist_ok=True)
            suggestions_dst = results_path / f"cwv_suggestions_{device}.json"
            shutil.copy2(suggestions_src, suggestions_dst)
            current_state["parsed_suggestions_path"] = str(suggestions_dst)
            current_state["parsed_suggestions_source_path"] = str(suggestions_src)
        else:
            current_state["parsed_suggestions_path"] = str(suggestions_src)

        report_src = result.get("report_path")
        if report_src:
            report_src_path = Path(report_src)
            if results_path:
                report_dst = results_path / f"cwv_report_{device}.md"
                shutil.copy2(report_src_path, report_dst)
                current_state["cwv_report_path"] = str(report_dst)
                current_state["cwv_report_source_path"] = str(report_src_path)
            else:
                current_state["cwv_report_path"] = str(report_src_path)

        logger.info(
            "CWV analysis complete. Suggestions saved at: %s",
            current_state.get("parsed_suggestions_path"),
        )
        return current_state

    return await run_with_timing("cwv_analysis", state, _impl)
