#!/usr/bin/env python3
"""
run_psi.py  –  Measure baseline CWV for HF dataset samples.

Full pipeline per sample (matches cwv-optimizer framework):
  clone/unzip repo → detect framework → install deps → serve locally
  → Cloudflare tunnel → PSI on tunnel URL → cleanup

Worker model (avoids Cloudflare rate-limiting):
  Each worker opens ONE persistent cloudflared tunnel at startup on a fixed
  port, then processes many samples sequentially through that tunnel.
  Between samples the server is swapped on the same port — the tunnel URL
  never changes.  Total tunnels created = --workers (default 4).

Wall-clock splits recorded per sample:
  repo_setup | install_deps | server_start | psi_mobile | psi_desktop |
  json_write | cleanup | sample_total

dataset_load is recorded once globally.

Full raw PSI JSON → <out_dir>/<idx>_<repo>/psi_{strategy}.json
Summary JSONL    → <out_dir>/summary.jsonl
Mean timings printed at end.

Usage:
    python scripts/helper_scripts/run_psi.py --both --workers 4 --limit 300
    python scripts/helper_scripts/run_psi.py --workers 1   # sequential
    python scripts/helper_scripts/run_psi.py --offset 14 --limit 286
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional

from tqdm import tqdm

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from psi_common import (
    CloudflaredTunnel,
    BoreTunnel,
    open_tunnel,
    call_psi,
    detect_framework,
    extract_metrics,
    find_available_port,
    get_deploy_commands,
    get_logger,
    run_install_commands,
    start_server,
    stop_server,
    PSI_API_KEY,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATASET_NAME    = "behavior-in-the-wild/cwv-bench-v0"
PSI_TIMEOUT     = 120
MAX_RETRIES     = 3
WORKER_PORT_GAP = 20   # ports per worker slot (room for retries)

# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------

def sanitize(s: str) -> str:
    return re.sub(r"[^\w\-.]", "_", s)


def setup_repo(repo_id: str, snapshots_dir: Path, workspace_dir: Path,
               logger: logging.Logger) -> Optional[Path]:
    safe     = sanitize(repo_id)
    target   = workspace_dir / safe
    zip_path = snapshots_dir / f"{safe}.zip"

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    if zip_path.exists():
        logger.info(f"  Unzipping {zip_path.name}")
        res = subprocess.run(["unzip", "-q", str(zip_path), "-d", str(target)],
                             capture_output=True)
        if res.returncode != 0:
            logger.error(f"  Unzip failed: {res.stderr.decode()[:200]}")
            return None
        items = list(target.iterdir())
        if len(items) == 1 and items[0].is_dir():
            for child in items[0].iterdir():
                shutil.move(str(child), str(target))
            items[0].rmdir()
        return target

    logger.info(f"  Cloning github.com/{repo_id}")
    res = subprocess.run(
        ["git", "clone", "--depth", "1",
         f"https://github.com/{repo_id}.git", str(target)],
        capture_output=True, timeout=120,
    )
    if res.returncode == 0:
        return target
    logger.error(f"  Clone failed: {res.stderr.decode()[:200]}")
    return None

# ---------------------------------------------------------------------------
# Worker — one persistent tunnel, many samples
# ---------------------------------------------------------------------------

def run_worker(
    worker_id: int,
    port: int,
    work_q: "queue.Queue[Optional[tuple]]",
    strategies: List[str],
    delay: float,
    api_key: str,
    out_dir: Path,
    snapshots_dir: Path,
    workspace_dir: Path,
    logger: logging.Logger,
    write_lock: threading.Lock,
    summary_file,
    timing_lock: threading.Lock,
    global_timing: Dict[str, List[float]],
    counters: Dict[str, int],
    pbar: tqdm,
    tunnel_provider: str = "cloudflare",
) -> None:
    """
    One worker thread.

    Lifecycle:
      1. Open a persistent tunnel on `port` using `tunnel_provider`.
      2. Drain work_q: for each sample clone → serve (on `port`) → PSI → cleanup server.
         The tunnel URL never changes between samples.
      3. Close the tunnel.
    """
    wlog = logger  # shared logger; every line includes [WKID] prefix via fmt

    # ── Open persistent tunnel ──────────────────────────────────────────────
    wlog.info(f"[W{worker_id}] Opening persistent {tunnel_provider} tunnel on port {port} …")
    tunnel = open_tunnel(tunnel_provider, port=port, logger=wlog)
    tunnel_url = tunnel.start()
    if not tunnel_url:
        wlog.error(f"[W{worker_id}] Could not establish tunnel — worker exiting.")
        # Drain the queue so we don't deadlock
        while True:
            item = work_q.get()
            if item is None:
                work_q.task_done()
                break
            sample_idx, _ = item
            wlog.warning(f"[W{worker_id}] [{sample_idx:04d}] SKIP (no tunnel)")
            with timing_lock:
                counters["skipped"] += 1
            pbar.update(1)
            work_q.task_done()
        return

    wlog.info(f"[W{worker_id}] Tunnel ready: {tunnel_url}")

    try:
        while True:
            item = work_q.get()
            if item is None:          # poison pill → shut down
                work_q.task_done()
                break

            sample_idx, row = item
            repo_id = row.get("REPO_ID", "") or row.get("repo_id", "")
            t_start = time.monotonic()
            timing: Dict[str, float] = {}

            wlog.info(f"[W{worker_id}] --- [{sample_idx:04d}] {repo_id} ---")

            def _skip(reason: str, extra_cleanup=None):
                if extra_cleanup:
                    extra_cleanup()
                with timing_lock:
                    counters["skipped"] += 1
                rec = {
                    "sample_idx": sample_idx, "repo_id": repo_id,
                    "ok": False, "skip_reason": reason,
                    "timing_s": timing, "results": {},
                }
                with write_lock:
                    summary_file.write(json.dumps(rec) + "\n")
                    summary_file.flush()
                pbar.update(1)
                work_q.task_done()

            # ── 1. Clone ──────────────────────────────────────────────────
            t0 = time.monotonic()
            repo_path = setup_repo(repo_id, snapshots_dir, workspace_dir, wlog)
            timing["repo_setup"] = round(time.monotonic() - t0, 3)
            if not repo_path:
                wlog.warning(f"[W{worker_id}] [{sample_idx:04d}] SKIP — clone/unzip failed")
                _skip("repo_setup")
                continue

            # ── 2. Detect framework + install ─────────────────────────────
            framework = detect_framework(repo_path)
            commands  = get_deploy_commands(framework, repo_path, port)
            wlog.info(f"[W{worker_id}] [{sample_idx:04d}] Framework: {framework} | Port: {port}")

            t0 = time.monotonic()
            ok, err = run_install_commands(commands, repo_path, wlog)
            timing["install_deps"] = round(time.monotonic() - t0, 3)
            if not ok:
                wlog.error(f"[W{worker_id}] [{sample_idx:04d}] SKIP — install failed: {err}")
                _skip("install_failed", lambda: shutil.rmtree(repo_path, ignore_errors=True))
                continue

            # ── 3. Start server (same port as tunnel) ─────────────────────
            t0 = time.monotonic()
            server_proc = start_server(commands[-1], repo_path, port, wlog)
            timing["server_start"] = round(time.monotonic() - t0, 3)
            if not server_proc:
                wlog.error(f"[W{worker_id}] [{sample_idx:04d}] SKIP — server failed")
                _skip("server_failed", lambda: shutil.rmtree(repo_path, ignore_errors=True))
                continue

            # ── 4. PSI (tunnel URL already open) ──────────────────────────
            sample_dir = out_dir / f"{sample_idx:04d}_{sanitize(repo_id)[:60]}"
            sample_dir.mkdir(parents=True, exist_ok=True)
            results: Dict[str, Any] = {}
            any_ok = False

            for strategy in strategies:
                if delay > 0:
                    time.sleep(delay)

                wlog.info(f"[W{worker_id}] [{sample_idx:04d}] PSI [{strategy}] → {tunnel_url}")
                t0 = time.monotonic()
                psi_json = call_psi(tunnel_url, strategy=strategy, api_key=api_key)
                psi_s = round(time.monotonic() - t0, 3)
                timing[f"psi_{strategy}"] = psi_s

                if psi_json:
                    tw = time.monotonic()
                    (sample_dir / f"psi_{strategy}.json").write_text(
                        json.dumps(psi_json, indent=2)
                    )
                    timing["json_write"] = timing.get("json_write", 0) + round(time.monotonic() - tw, 3)
                    metrics = extract_metrics(psi_json)
                    results[strategy] = metrics
                    perf    = metrics["scores"].get("performance", "?")
                    lcp     = metrics["metrics"].get("LCP")
                    lcp_str = f"{round(lcp):,}ms" if lcp is not None else "?"
                    wlog.info(f"[W{worker_id}] [{sample_idx:04d}] ✓ {strategy:<8} "
                              f"psi={psi_s:.1f}s  perf={perf}  LCP={lcp_str}")
                    any_ok = True
                else:
                    wlog.error(f"[W{worker_id}] [{sample_idx:04d}] ✗ {strategy:<8} PSI returned nothing")
                    results[strategy] = None

            # ── 5. Stop server (tunnel stays open) ────────────────────────
            t0 = time.monotonic()
            stop_server(server_proc, wlog)
            shutil.rmtree(repo_path, ignore_errors=True)
            timing["cleanup"] = round(time.monotonic() - t0, 3)
            timing["sample_total"] = round(time.monotonic() - t_start, 3)

            wlog.info(f"[W{worker_id}] [{sample_idx:04d}] total={timing['sample_total']:.1f}s")

            # ── Persist result ─────────────────────────────────────────────
            rec = {
                "sample_idx": sample_idx,
                "repo_id":    repo_id,
                "framework":  framework,
                "tunnel_url": tunnel_url,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "timing_s":   timing,
                "results":    results,
                "ok":         any_ok,
            }
            with write_lock:
                summary_file.write(json.dumps(rec) + "\n")
                summary_file.flush()
            with timing_lock:
                for phase, val in timing.items():
                    global_timing[phase].append(val)
                if any_ok:
                    counters["success"] += 1
                else:
                    counters["skipped"] += 1

            pbar.update(1)
            pbar.set_postfix(ok=counters["success"], skip=counters["skipped"], refresh=False)
            work_q.task_done()

    finally:
        wlog.info(f"[W{worker_id}] Closing tunnel.")
        tunnel.stop()


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run baseline PSI on HF dataset URLs "
                    "(persistent-tunnel workers: clone → deploy → PSI → cleanup server)"
    )
    p.add_argument("--limit",    type=int,   default=300,
                   help="Number of samples (default: 300, -1 for all)")
    p.add_argument("--offset",   type=int,   default=0,
                   help="Dataset start index (default: 0)")
    p.add_argument("--both",     action="store_true",
                   help="Run both mobile and desktop")
    p.add_argument("--strategy", choices=["mobile", "desktop"], default="mobile",
                   help="Device strategy (ignored if --both)")
    p.add_argument("--workers",  type=int,   default=4,
                   help="Number of parallel workers / tunnels (default: 4)")
    p.add_argument("--delay",    type=float, default=0.5,
                   help="Delay between PSI calls within one sample (default: 0.5s)")
    p.add_argument("--api-key",  default=PSI_API_KEY,
                   help="Google PSI API key (default: $GOOGLE_PAGESPEED_INSIGHTS_API_KEY)")
    p.add_argument("--out",      default=None,
                   help="Output directory (default: logs/psi_run/<ts>)")
    p.add_argument(
        "--tunnel-provider",
        choices=["cloudflare", "bore"],
        default="cloudflare",
        help=(
            "Tunnel provider to expose local servers to Google PSI. "
            "'cloudflare' (default) gives HTTPS on port 443 — works through "
            "all firewalls. 'bore' is faster to provision but uses HTTP on a "
            "random high port (may be blocked by corporate firewalls)."
        ),
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if not args.api_key:
        sys.exit(
            "ERROR: GOOGLE_PAGESPEED_INSIGHTS_API_KEY is not set.\n"
            "Export it or pass it via --api-key."
        )

    # ── Paths ──────────────────────────────────────────────────────────────
    project_root  = Path(__file__).resolve().parents[2]
    ts            = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir       = Path(args.out) if args.out else project_root / "logs" / "psi_run" / ts
    snapshots_dir = project_root / "harness" / "REPO_SNAPSHOTS"
    workspace_dir = project_root / "tmp_psi_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    summary_path = out_dir / "summary.jsonl"
    log_path     = out_dir / "run.log"
    strategies   = ["mobile", "desktop"] if args.both else [args.strategy]

    logger = get_logger("run_psi", log_path)
    logger.info(f"PSI run | limit={args.limit} offset={args.offset} "
                f"strategies={strategies} workers={args.workers} "
                f"tunnel={args.tunnel_provider}")
    logger.info(f"Output      : {out_dir}")
    logger.info(f"API key set : {'yes' if args.api_key else 'NO'}")
    logger.info(
        f"Worker model: {args.workers} persistent {args.tunnel_provider} tunnels, "
        f"samples distributed via queue"
    )

    # ── Load dataset ───────────────────────────────────────────────────────
    t0 = time.monotonic()
    logger.info("Loading HF dataset …")
    from datasets import load_dataset
    dataset  = load_dataset(DATASET_NAME, split="train")
    limit    = len(dataset) if args.limit == -1 else args.limit
    end_idx  = min(args.offset + limit, len(dataset))
    samples  = list(dataset.select(range(args.offset, end_idx)))
    dl_s     = round(time.monotonic() - t0, 2)
    logger.info(f"Dataset loaded in {dl_s}s — {len(samples)} samples "
                f"(rows {args.offset}–{end_idx - 1})")

    # ── Shared state ───────────────────────────────────────────────────────
    write_lock    = threading.Lock()
    timing_lock   = threading.Lock()
    global_timing: Dict[str, list] = defaultdict(list)
    global_timing["dataset_load"].append(dl_s)
    counters = {"success": 0, "skipped": 0}

    # ── Build work queue ───────────────────────────────────────────────────
    work_q: queue.Queue = queue.Queue()
    for i, row in enumerate(samples):
        work_q.put((args.offset + i, dict(row)))
    # One poison pill per worker
    for _ in range(args.workers):
        work_q.put(None)

    # ── Stagger tunnel startup to avoid simultaneous registration spikes ───
    # Workers are launched in threads but each delays slightly before connecting.
    TUNNEL_STAGGER_S = 5  # seconds between each worker opening its tunnel

    pbar = tqdm(total=len(samples), desc="PSI", unit="sample")

    # ── Launch workers ─────────────────────────────────────────────────────
    threads = []
    with open(summary_path, "a", encoding="utf-8") as summary_file:
        for wid in range(args.workers):
            port = find_available_port(8200 + wid * WORKER_PORT_GAP)

            def _worker(worker_id=wid, worker_port=port):
                # Stagger tunnel openings
                if worker_id > 0:
                    time.sleep(worker_id * TUNNEL_STAGGER_S)
                run_worker(
                    worker_id       = worker_id,
                    port            = worker_port,
                    work_q          = work_q,
                    strategies      = strategies,
                    delay           = args.delay,
                    api_key         = args.api_key,
                    out_dir         = out_dir,
                    snapshots_dir   = snapshots_dir,
                    workspace_dir   = workspace_dir,
                    logger          = logger,
                    write_lock      = write_lock,
                    summary_file    = summary_file,
                    timing_lock     = timing_lock,
                    global_timing   = global_timing,
                    counters        = counters,
                    pbar            = pbar,
                    tunnel_provider = args.tunnel_provider,
                )

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

    pbar.close()

    logger.info(
        f"\nDone: {counters['success']} succeeded, "
        f"{counters['skipped']} failed/skipped out of {len(samples)}"
    )
    logger.info(f"Full JSON reports : {out_dir}/")
    logger.info(f"Summary JSONL     : {summary_path}")

    # ── Timing summary ─────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("MEAN WALL-CLOCK TIME PER SPLIT (seconds)")
    print("=" * 60)
    for phase in [
        "repo_setup", "install_deps", "server_start",
        "psi_mobile", "psi_desktop", "json_write",
        "cleanup", "sample_total", "dataset_load",
    ]:
        vals = global_timing.get(phase)
        if vals:
            print(f"  {phase:<20} n={len(vals):>4}   mean={mean(vals):>7.2f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
