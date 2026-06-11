from __future__ import annotations

import contextlib
import json
import os
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
