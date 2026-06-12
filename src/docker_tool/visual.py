from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docker_tool.measurement import MEASURE_IMAGE, ROOT_DIR, _rewrite_url_for_host_gateway
from docker_tool.resources import SlotLease, docker_resource_args, host_load


VISUAL_IMAGE = os.getenv("WEB_BENCH_VISUAL_IMAGE", "web-bench/visual:latest")
SECRET_ENV_PREFIXES = (
    "AZURE_OPENAI_API_KEY=",
    "AZURE_OPENAI_ENDPOINT=",
    "AZURE_DEPLOYMENT=",
    "OPENAI_API_KEY=",
)


def _redact_command(cmd: list[str]) -> list[str]:
    redacted: list[str] = []
    for part in cmd:
        if any(part.startswith(prefix) for prefix in SECRET_ENV_PREFIXES):
            redacted.append(part.split("=", 1)[0] + "=<redacted>")
        else:
            redacted.append(part)
    return redacted


@dataclass
class VisualResult:
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    command: list[str] | None = None


def run_visual_in_docker(
    *,
    url: str,
    screenshot_path: Path,
    repo_id: str,
    commit_id: str,
    framework: str,
    host_file_path: str,
    patch_file: Path | None,
    output_json: Path,
    host_container_id: str | None = None,
    slot: SlotLease | None = None,
) -> VisualResult:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    load_before = host_load()

    container_url = url if host_container_id else _rewrite_url_for_host_gateway(url)
    resource_args, resource_policy = docker_resource_args(slot, workload="visual")
    cmd = [
        "docker", "run", "--rm", "-i",
        "-e", "PYTHONPATH=/repo/src:/repo/src/regression_tool",
        "-e", "CWV_DOCKER_BROWSER=1",
        "-e", "SANDBOX_MODE=local",
        "-e", f"AZURE_OPENAI_API_KEY={os.getenv('AZURE_OPENAI_API_KEY', '')}",
        "-e", f"AZURE_OPENAI_ENDPOINT={os.getenv('AZURE_OPENAI_ENDPOINT', '')}",
        "-e", f"AZURE_DEPLOYMENT={os.getenv('AZURE_DEPLOYMENT', '')}",
        "-e", f"OPENAI_API_VERSION={os.getenv('OPENAI_API_VERSION', '2024-02-15-preview')}",
    ]
    for key, value in sorted(os.environ.items()):
        if key.startswith("REGRESSION_"):
            cmd.extend(["-e", f"{key}={value}"])
    cmd.extend([
        "--mount", f"type=bind,src={ROOT_DIR.resolve()},dst=/repo,ro",
        "--mount", f"type=bind,src={output_json.parent.resolve()},dst=/out",
        *resource_args,
    ])
    if host_container_id:
        cmd.extend(["--network", f"container:{host_container_id}"])
    else:
        cmd.extend(["--add-host", "host.docker.internal:host-gateway"])
    args = [
        "python3", "/repo/src/regression_tool/visual_validate.py",
        "--url", container_url,
        "--screenshot-path", f"/out/{screenshot_path.name}",
        "--repo-id", repo_id,
        "--commit-id", commit_id or "",
        "--framework", framework,
        "--host-file-path", host_file_path or "",
        "--output-json", f"/out/{output_json.name}",
    ]
    if patch_file is not None:
        patch_resolved = patch_file.resolve()
        try:
            patch_arg = f"/out/{patch_resolved.relative_to(output_json.parent.resolve())}"
        except ValueError:
            try:
                patch_arg = f"/repo/{patch_resolved.relative_to(ROOT_DIR.resolve())}"
            except ValueError as exc:
                return VisualResult(
                    status="error",
                    error=f"patch file must be inside repo or output dir for docker visual: {patch_resolved}",
                    command=_redact_command(cmd),
                )
        args.extend(["--patch-file", patch_arg])
    if slot is not None:
        args.extend(["--slot-json", json.dumps(slot.to_dict())])
    cmd.extend([VISUAL_IMAGE, *args])

    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        err = run.stderr.strip() or run.stdout.strip() or f"docker visual exited {run.returncode}"
        return VisualResult(status="error", error=err, command=_redact_command(cmd))
    try:
        result = json.loads(output_json.read_text(encoding="utf-8"))
    except Exception as exc:
        return VisualResult(status="error", error=f"visual output missing/invalid: {exc}", command=_redact_command(cmd))

    result.setdefault("tool", "regression")
    result.setdefault("status", "success" if result.get("error") is None else "invalid_eval")
    result.setdefault("sandbox", {})
    result["sandbox"].update({
        "enabled": True,
        "mode": "docker-visual",
        "host_container_id": host_container_id,
        "measurement_image": MEASURE_IMAGE,
        "visual_image": VISUAL_IMAGE,
        "url_from_host": url,
        "url_from_visual_container": container_url,
        "resource_slot": slot.to_dict() if slot else None,
        "resource_policy": resource_policy,
        "host_load_before": load_before,
        "host_load_after": host_load(),
    })
    output_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result.get("error") is not None or result.get("status") not in {None, "success", "valid"}:
        return VisualResult(status="error", result=result, command=_redact_command(cmd))
    return VisualResult(status="success", result=result, command=_redact_command(cmd))
