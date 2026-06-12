from __future__ import annotations

import contextlib
import json
import os
import platform
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class SlotLease:
    slot_id: int
    cpuset: str
    cpu_count: int
    memory: str
    queue_wait_ms: int
    mode: str = "local"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DockerResourcePolicy:
    enabled: bool
    linux_controls: bool
    workload: str
    cpuset: str | None
    cpu_count: int | None
    cpus: str | None
    cpu_shares: str | None
    memory: str
    memory_swap: str | None
    shm_size: str
    pids_limit: str
    cpuset_mems: str | None
    cgroup_parent: str | None
    ulimits: list[str]
    init: bool
    no_new_privileges: bool
    cap_drop: str | None
    oom_score_adj: str | None
    platform: str

    def to_dict(self) -> dict:
        return asdict(self)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def docker_resource_policy(slot: SlotLease | None, workload: str = "generic") -> DockerResourcePolicy:
    """Build Docker resource controls without changing page/browser loading behavior."""
    enabled = _env_bool("SANDBOX_DOCKER_RESOURCE_CONTROLS", True)
    host_platform = platform.system().lower()
    linux_controls = enabled and (
        host_platform == "linux" or _env_bool("SANDBOX_FORCE_LINUX_CONTROLS", False)
    )
    cpu_count = slot.cpu_count if slot else None
    cpus_env = os.getenv("SANDBOX_DOCKER_CPUS")
    cpus_value = cpus_env if cpus_env is not None else (str(max(1, cpu_count)) if cpu_count else None)
    cpu_shares_env = os.getenv("SANDBOX_CPU_SHARES")
    cpu_shares_value = cpu_shares_env if cpu_shares_env is not None else (str(1024 * max(1, cpu_count)) if cpu_count else None)
    memory = slot.memory if slot else os.getenv("SANDBOX_SLOT_MEMORY", "4g")
    memory_swap = os.getenv("SANDBOX_MEMORY_SWAP")
    if linux_controls and memory_swap is None and _env_bool("SANDBOX_LOCK_MEMORY_SWAP", True):
        memory_swap = memory
    return DockerResourcePolicy(
        enabled=enabled,
        linux_controls=linux_controls,
        workload=workload,
        cpuset=slot.cpuset if slot and slot.cpuset else None,
        cpu_count=cpu_count,
        cpus=cpus_value,
        cpu_shares=cpu_shares_value if linux_controls else None,
        memory=memory,
        memory_swap=memory_swap if linux_controls else os.getenv("SANDBOX_MEMORY_SWAP"),
        shm_size=os.getenv("SANDBOX_SHM_SIZE", "1g"),
        pids_limit=os.getenv("SANDBOX_PIDS_LIMIT", "512"),
        cpuset_mems=os.getenv("SANDBOX_CPUSET_MEMS") if linux_controls else None,
        cgroup_parent=os.getenv("SANDBOX_CGROUP_PARENT") if linux_controls else None,
        ulimits=[
            item.strip()
            for item in os.getenv("SANDBOX_ULIMITS", "nofile=262144:262144,nproc=4096:4096").split(",")
            if item.strip()
        ] if linux_controls else [],
        init=_env_bool("SANDBOX_DOCKER_INIT", True),
        no_new_privileges=_env_bool("SANDBOX_NO_NEW_PRIVILEGES", True) if linux_controls else False,
        cap_drop=os.getenv("SANDBOX_CAP_DROP") if linux_controls else None,
        oom_score_adj=os.getenv("SANDBOX_OOM_SCORE_ADJ") if linux_controls else None,
        platform=host_platform,
    )


def docker_resource_args(slot: SlotLease | None, workload: str = "generic") -> tuple[list[str], dict]:
    policy = docker_resource_policy(slot, workload=workload)
    if not policy.enabled:
        return [], policy.to_dict()

    args: list[str] = []
    if policy.init:
        args.append("--init")
    args.extend(["--memory", policy.memory])
    if policy.memory_swap:
        args.extend(["--memory-swap", policy.memory_swap])
    if policy.shm_size:
        args.extend(["--shm-size", policy.shm_size])
    if policy.pids_limit:
        args.extend(["--pids-limit", policy.pids_limit])
    if policy.cpuset:
        args.extend(["--cpuset-cpus", policy.cpuset])
    if policy.cpus:
        args.extend(["--cpus", policy.cpus])
    if policy.cpu_shares:
        args.extend(["--cpu-shares", policy.cpu_shares])
    if policy.cpuset_mems:
        args.extend(["--cpuset-mems", policy.cpuset_mems])
    if policy.cgroup_parent:
        args.extend(["--cgroup-parent", policy.cgroup_parent])
    for ulimit in policy.ulimits:
        args.extend(["--ulimit", ulimit])
    if policy.no_new_privileges:
        args.extend(["--security-opt", "no-new-privileges:true"])
    if policy.cap_drop:
        args.extend(["--cap-drop", policy.cap_drop])
    if policy.oom_score_adj:
        args.extend(["--oom-score-adj", policy.oom_score_adj])
    return args, policy.to_dict()


class SlotScheduler:
    """File-lock backed resource-slot scheduler for cross-process CWV admission control."""

    def __init__(
        self,
        slots: int | None = None,
        cpus_per_slot: int | None = None,
        reserve_cpus: int | None = None,
        memory: str | None = None,
        lock_dir: Path | None = None,
    ) -> None:
        affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else list(range(os.cpu_count() or 1))
        self._cpus = affinity
        self.reserve_cpus = max(0, reserve_cpus if reserve_cpus is not None else int(os.getenv("SANDBOX_RESERVE_CPUS", "4")))
        self.cpus_per_slot = max(1, cpus_per_slot if cpus_per_slot is not None else int(os.getenv("SANDBOX_CPUS_PER_SLOT", "4")))
        usable = max(1, len(affinity) - self.reserve_cpus)
        computed_slots = max(1, usable // self.cpus_per_slot)
        env_slots = os.getenv("SANDBOX_MAX_SLOTS", "20")
        requested_slots = slots if slots is not None else int(env_slots)
        self.strict = os.getenv("SANDBOX_STRICT_SLOTS", "1").strip().lower() not in {"0", "false", "no"}
        if self.strict and requested_slots > computed_slots:
            raise ValueError(
                f"requested {requested_slots} slots but only {computed_slots} exclusive slots "
                f"available with {len(affinity)} CPUs, reserve_cpus={self.reserve_cpus}, "
                f"cpus_per_slot={self.cpus_per_slot}"
            )
        self.slots = max(1, min(requested_slots, computed_slots))
        self.memory = memory or os.getenv("SANDBOX_SLOT_MEMORY", "4g")
        self.lock_dir = lock_dir or Path(os.getenv("SANDBOX_LOCK_DIR", "/tmp/web_bench_cwv_slots"))
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def _slot_cpus(self, slot_id: int) -> list[int]:
        start = slot_id * self.cpus_per_slot
        usable = self._cpus[: max(1, len(self._cpus) - self.reserve_cpus)] or self._cpus
        if start >= len(usable):
            start = (slot_id * self.cpus_per_slot) % len(usable)
        cpus = usable[start : start + self.cpus_per_slot]
        return cpus or [usable[start % len(usable)]]

    def lease_for_index(self, slot_index: int, mode: str = "docker") -> SlotLease:
        if self.strict and slot_index >= self.slots:
            raise ValueError(f"slot index {slot_index} exceeds available slots {self.slots}")
        slot_id = slot_index % self.slots
        cpus = self._slot_cpus(slot_id)
        return SlotLease(
            slot_id=slot_id,
            cpuset=",".join(str(c) for c in cpus),
            cpu_count=len(cpus),
            memory=self.memory,
            queue_wait_ms=0,
            mode=mode,
        )

    @contextlib.contextmanager
    def acquire(self, mode: str = "local") -> Iterator[SlotLease]:
        import fcntl

        start_wait = time.time()
        files: list[object] = []
        while True:
            for slot_id in range(self.slots):
                path = self.lock_dir / f"slot_{slot_id}.lock"
                f = path.open("w")
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    cpus = self._slot_cpus(slot_id)
                    lease = SlotLease(
                        slot_id=slot_id,
                        cpuset=",".join(str(c) for c in cpus),
                        cpu_count=len(cpus),
                        memory=self.memory,
                        queue_wait_ms=int((time.time() - start_wait) * 1000),
                        mode=mode,
                    )
                    f.write(json.dumps(lease.to_dict()))
                    f.flush()
                    files.append(f)
                    try:
                        yield lease
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        f.close()
                    return
                except BlockingIOError:
                    f.close()
                    continue
            time.sleep(0.25)


def host_load() -> dict:
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    return {
        "load1": round(load1, 4),
        "load5": round(load5, 4),
        "load15": round(load15, 4),
        "cpu_count": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1),
    }


@contextlib.contextmanager
def bind_process_to_slot(slot: SlotLease | None) -> Iterator[None]:
    """Temporarily bind the current process, and inherited children, to a slot cpuset."""
    if slot is None or not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        yield
        return
    original = os.sched_getaffinity(0)
    try:
        cpus = {int(part) for part in slot.cpuset.split(",") if part.strip()}
        if cpus:
            os.sched_setaffinity(0, cpus)
        yield
    finally:
        try:
            os.sched_setaffinity(0, original)
        except OSError:
            pass
