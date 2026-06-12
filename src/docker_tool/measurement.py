from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docker_tool.resources import SlotLease, docker_resource_args, host_load


ROOT_DIR = Path(__file__).resolve().parents[2]
MEASURE_IMAGE = os.getenv("WEB_BENCH_MEASURE_IMAGE", "web-bench/base:latest")


@dataclass
class MeasureResult:
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    command: list[str] | None = None


INNER_SCRIPT = r"""
import asyncio
import json
import logging
import os
import sys
import time

from cwv_tool.performance_testing import measure_multiple_runs, calculate_aggregated_metrics


def _to_stderr(lgr: logging.Logger) -> None:
    for handler in lgr.handlers[:]:
        if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is sys.stdout:
            lgr.removeHandler(handler)
            new_handler = logging.StreamHandler(sys.stderr)
            new_handler.setLevel(handler.level)
            new_handler.setFormatter(handler.formatter)
            lgr.addHandler(new_handler)


_to_stderr(logging.root)
for logger_obj in list(logging.Logger.manager.loggerDict.values()):
    if isinstance(logger_obj, logging.Logger):
        _to_stderr(logger_obj)

url = os.environ["CWV_URL"]
device = os.environ["CWV_DEVICE"]
num_runs = int(os.environ.get("CWV_NUM_RUNS", "5"))
headless = os.environ.get("CWV_HEADED", "0").strip().lower() not in {"1", "true", "yes"}

start = time.time()
runs, final_settle_time, success = asyncio.run(
    measure_multiple_runs(url=url, device=device, headless=headless, num_runs=num_runs)
)
aggregated = calculate_aggregated_metrics(runs)
lcp_elements = [r.get("lcp_element") if r.get("status") == "success" else None for r in runs]
cls_shifts = [r.get("cls_shifts") if r.get("status") == "success" else [] for r in runs]
inp_interactions = [r.get("inp_interactions") if r.get("status") == "success" else [] for r in runs]

print(json.dumps({
    "status": "success" if success else "error",
    "tool": "cwv",
    "runs": runs,
    "aggregated": aggregated,
    "lcp_element": lcp_elements,
    "cls_shifts": cls_shifts,
    "inp_interactions": inp_interactions,
    "num_runs": num_runs,
    "device": device,
    "final_settle_time": final_settle_time,
    "sandbox": {
        "enabled": True,
        "mode": "docker-measure",
        "measurement_wall_ms": int((time.time() - start) * 1000),
        "inside_container": True,
    },
}, default=str))
"""


def _rewrite_url_for_host_gateway(url: str) -> str:
    return (
        url.replace("http://127.0.0.1:", "http://host.docker.internal:")
        .replace("http://localhost:", "http://host.docker.internal:")
        .replace("https://127.0.0.1:", "https://host.docker.internal:")
        .replace("https://localhost:", "https://host.docker.internal:")
    )


def measure_in_docker(
    url: str,
    device: str,
    num_runs: int,
    host_container_id: str | None = None,
    slot: SlotLease | None = None,
    headed: bool = False,
) -> MeasureResult:
    load_before = host_load()
    container_url = url if host_container_id else _rewrite_url_for_host_gateway(url)
    resource_args, resource_policy = docker_resource_args(slot, workload=f"cwv-{device}")
    cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "-e",
        "PYTHONPATH=/repo/src",
        "-e",
        "CWV_DOCKER_BROWSER=1",
        "-e",
        f"CWV_URL={container_url}",
        "-e",
        f"CWV_DEVICE={device}",
        "-e",
        f"CWV_NUM_RUNS={num_runs}",
        "-e",
        f"CWV_HEADED={1 if headed else 0}",
        "--mount",
        f"type=bind,src={ROOT_DIR.resolve()},dst=/repo,ro",
        *resource_args,
    ]
    if host_container_id:
        cmd.extend(["--network", f"container:{host_container_id}"])
    else:
        cmd.extend(["--add-host", "host.docker.internal:host-gateway"])
    cmd.extend([MEASURE_IMAGE, "python3", "-c", INNER_SCRIPT])

    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        return MeasureResult(
            status="error",
            error=(run.stderr.strip() or run.stdout.strip() or f"docker exited {run.returncode}"),
            command=cmd,
        )
    try:
        result = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        return MeasureResult(status="error", error=f"invalid JSON from measurement container: {exc}; stdout={run.stdout[:1000]!r}", command=cmd)

    result.setdefault("sandbox", {})
    result.setdefault("tool", "cwv")
    result.setdefault("status", "success")
    result["sandbox"].update(
        {
            "enabled": True,
            "mode": "docker-measure",
            "host_container_id": host_container_id,
            "measurement_image": MEASURE_IMAGE,
            "url_from_host": url,
            "url_from_measure_container": container_url,
            "resource_slot": slot.to_dict() if slot else None,
            "resource_policy": resource_policy,
            "host_load_before": load_before,
            "host_load_after": host_load(),
        }
    )
    return MeasureResult(status="success", result=result, command=cmd)
