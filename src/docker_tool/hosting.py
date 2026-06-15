from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from docker_tool.frameworks import all_images, normalize_framework
from docker_tool.resources import SlotLease, docker_resource_args, docker_resource_policy


ROOT_DIR = Path(__file__).resolve().parents[2]
HARNESS_DIR = ROOT_DIR / "harness"
ENTRYPOINT = Path(__file__).resolve().parent / "runtime" / "host_entrypoint.sh"


@dataclass
class HostResult:
    status: str
    url: str | None = None
    mode: str = "local"
    framework: str = "static"
    port: int | None = None
    pid: int | None = None
    container_id: str | None = None
    image: str | None = None
    error: str | None = None
    resource_slot: dict[str, Any] | None = None
    resource_policy: dict[str, Any] | None = None
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def docker_available() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker executable not found"
    result = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip() or "docker daemon not reachable"
    return True, ""


def image_exists(image: str) -> bool:
    result = subprocess.run(["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def _wait_for_server(port: int, timeout: int = 90) -> bool:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                if 200 <= response.status < 500:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _local_preexec(slot: SlotLease | None):
    def _setup() -> None:
        os.setsid()
        if slot and hasattr(os, "sched_setaffinity"):
            cpus = {int(part) for part in slot.cpuset.split(",") if part.strip()}
            if cpus:
                os.sched_setaffinity(0, cpus)

    return _setup


def _local_start(
    repo_dir: Path,
    framework: str,
    host_file_path: str | None,
    port: int,
    log: Path,
    slot: SlotLease | None,
) -> HostResult:
    try:
        spec = normalize_framework(framework, host_file_path)
    except ValueError as exc:
        return HostResult(status="error", mode="local", framework=framework or "", port=port, error=str(exc))
    script = HARNESS_DIR / "host_files" / spec.legacy_script
    if host_file_path:
        candidate = HARNESS_DIR / host_file_path
        if candidate.exists():
            script = candidate
    if not script.exists():
        return HostResult(status="error", mode="local", framework=spec.key, port=port, error=f"host script not found: {script}")

    if spec.key == "static":
        for src_name, dst_name in (
            ("http2_server.js", "http2_server.cjs"),
            ("localhost-key.pem", "localhost-key.pem"),
            ("localhost-cert.pem", "localhost-cert.pem"),
        ):
            src = HARNESS_DIR / "host_files" / src_name
            dst = repo_dir / dst_name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)

    log.parent.mkdir(parents=True, exist_ok=True)
    log_file = log.open("ab")
    env = {**os.environ, "PORT": str(port)}
    proc = subprocess.Popen(
        ["bash", str(script), str(repo_dir), str(log)],
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        preexec_fn=_local_preexec(slot),
    )
    if not _wait_for_server(port):
        stop_host(pid=proc.pid)
        return HostResult(status="error", mode="local", framework=spec.key, port=port, pid=proc.pid, error="server startup timeout")
    return HostResult(
        status="success",
        mode="local",
        framework=spec.key,
        port=port,
        pid=proc.pid,
        url=f"http://127.0.0.1:{port}",
        resource_slot=slot.to_dict() if slot else None,
    )


def _docker_start(
    repo_dir: Path,
    framework: str,
    host_file_path: str | None,
    port: int,
    log: Path,
    slot: SlotLease | None,
) -> HostResult:
    try:
        spec = normalize_framework(framework, host_file_path)
    except ValueError as exc:
        return HostResult(status="error", mode="docker", framework=framework or "", port=port, error=str(exc))
    ok, err = docker_available()
    if not ok:
        return HostResult(status="error", mode="docker", framework=spec.key, port=port, image=spec.image, error=err)
    if not image_exists(spec.image):
        return HostResult(status="error", mode="docker", framework=spec.key, port=port, image=spec.image, error=f"missing image: {spec.image}")

    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch(exist_ok=True)
    cache_root = Path(os.getenv("WEB_BENCH_DOCKER_CACHE", str(Path.home() / ".cache" / "web_bench_docker")))
    cache_root.mkdir(parents=True, exist_ok=True)
    name = f"web-bench-host-{os.getpid()}-{int(time.time() * 1000)}"
    resource_args, resource_policy = docker_resource_args(slot, workload=f"host-{spec.key}")
    cmd = [
        "docker", "run", "--rm", "-d",
        "--name", name,
        "--network", "bridge",
        "-p", f"127.0.0.1:{port}:{port}",
        "-e", f"PORT={port}",
        "-e", f"FRAMEWORK={spec.key}",
        "-e", f"HOST_FILE_PATH={host_file_path or ''}",
        "--mount", f"type=bind,src={repo_dir.resolve()},dst=/workspace",
        "--mount", f"type=bind,src={log.resolve()},dst=/var/log/web-bench-host.log",
        "--mount", f"type=bind,src={ENTRYPOINT.resolve()},dst=/usr/local/bin/web-bench-host,ro",
        "--mount", f"type=bind,src={cache_root.resolve()},dst=/cache",
        *resource_args,
    ]
    http2_server = HARNESS_DIR / "host_files" / "http2_server.js"
    cert = HARNESS_DIR / "host_files" / "localhost-cert.pem"
    key = HARNESS_DIR / "host_files" / "localhost-key.pem"
    if http2_server.exists():
        cmd.extend(["--mount", f"type=bind,src={http2_server.resolve()},dst=/usr/local/bin/http2_server.js,ro"])
    if cert.exists():
        cmd.extend(["--mount", f"type=bind,src={cert.resolve()},dst=/usr/local/bin/localhost-cert.pem,ro"])
    if key.exists():
        cmd.extend(["--mount", f"type=bind,src={key.resolve()},dst=/usr/local/bin/localhost-key.pem,ro"])
    cmd.extend([spec.image, "bash", "/usr/local/bin/web-bench-host"])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return HostResult(status="error", mode="docker", framework=spec.key, port=port, image=spec.image, error=result.stderr.strip(), command=cmd)
    cid = result.stdout.strip()
    if not _wait_for_server(port):
        stop_host(container_id=cid)
        return HostResult(status="error", mode="docker", framework=spec.key, port=port, image=spec.image, container_id=cid, error="server startup timeout", command=cmd)
    return HostResult(
        status="success",
        mode="docker",
        framework=spec.key,
        port=port,
        image=spec.image,
        container_id=cid,
        url=f"http://127.0.0.1:{port}",
        resource_slot=slot.to_dict() if slot else None,
        resource_policy=resource_policy,
        command=cmd,
    )


def start_host(
    repo_dir: str | Path,
    framework: str,
    port: int,
    log: str | Path,
    host_file_path: str | None = None,
    mode: str = "auto",
    slot: SlotLease | None = None,
) -> HostResult:
    repo_path = Path(repo_dir)
    log_path = Path(log)
    requested = (mode or "auto").lower()
    if requested not in {"auto", "docker", "local"}:
        return HostResult(status="error", error=f"unknown mode: {mode}", port=port)

    if requested in {"auto", "docker"}:
        result = _docker_start(repo_path, framework, host_file_path, port, log_path, slot)
        if requested == "auto" and result.status == "error":
            result = _local_start(repo_path, framework, host_file_path, port, log_path, slot)
        return result
    return _local_start(repo_path, framework, host_file_path, port, log_path, slot)


def stop_host(container_id: str | None = None, pid: int | None = None) -> None:
    if container_id:
        subprocess.run(["docker", "rm", "-f", container_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass


def doctor() -> dict[str, Any]:
    ok, err = docker_available()
    images = {image: image_exists(image) if ok else False for image in all_images()}
    return {
        "docker_available": ok,
        "docker_error": err,
        "images": images,
        "entrypoint": str(ENTRYPOINT),
        "entrypoint_exists": ENTRYPOINT.exists(),
        "resource_policy": docker_resource_policy(None, workload="doctor").to_dict(),
    }


def result_from_json(text: str) -> HostResult:
    return HostResult(**json.loads(text))
