#!/usr/bin/env python3
"""
Bulk PSI Run with Cloudflare Tunneling.

Exposes local GitHub Pages deployments via Cloudflare quick tunnels, runs
Google PageSpeed Insights on each, and records:
  - CWV metrics (LCP, CLS, INP, FCP, TTFB, TBT, SpeedIndex)
  - Category scores (performance, accessibility, best-practices, seo)
  - Per-sample wall-clock timing (deploy → tunnel → PSI → teardown)
  - Rate-limit events (HTTP 429) to help determine safe request cadence

Results are written to a JSONL file (one JSON object per line).
A summary with avg/p50/p95 timing is printed at the end.

Usage:
    python bulk_psi_run_tunneling.py --limit 50 --out results.jsonl
    python bulk_psi_run_tunneling.py --limit 300 --delay 5 --api-key $KEY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, quantiles
from typing import Any, Dict, List, Optional

from tqdm import tqdm

# Shared utilities (tunnel, PSI, server deploy, framework detection)
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
    PSI_URL,
    PSI_API_KEY,
    PSI_ADOBE_URL,
)

# ---------------------------------------------------------------------------
# Repo setup (zip or git clone)
# ---------------------------------------------------------------------------

def sanitize_repo_id(repo_id: str) -> str:
    return re.sub(r"[^\w\-.]", "_", repo_id)


def setup_repo(
    repo_id: str,
    snapshots_dir: Path,
    workspace_dir: Path,
    logger,
) -> Optional[Path]:
    """Unzip a snapshot or shallow-clone the repo. Returns the repo directory."""
    safe = sanitize_repo_id(repo_id)
    zip_path = snapshots_dir / f"{safe}.zip"
    target = workspace_dir / safe

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    if zip_path.exists():
        logger.info(f"Unzipping {zip_path.name}")
        res = subprocess.run(
            ["unzip", "-q", str(zip_path), "-d", str(target)],
            capture_output=True,
        )
        if res.returncode != 0:
            logger.error(f"Unzip failed: {res.stderr.decode()[:200]}")
            return None
        # Flatten single top-level directory
        items = list(target.iterdir())
        if len(items) == 1 and items[0].is_dir():
            for child in items[0].iterdir():
                shutil.move(str(child), str(target))
            items[0].rmdir()
        return target

    # Fallback: shallow clone
    logger.info(f"Cloning github.com/{repo_id}")
    res = subprocess.run(
        ["git", "clone", "--depth", "1", f"https://github.com/{repo_id}.git", str(target)],
        capture_output=True,
    )
    if res.returncode == 0:
        return target
    logger.error(f"Clone failed: {res.stderr.decode()[:200]}")
    return None

# ---------------------------------------------------------------------------
# Rate-limit aware PSI wrapper
# ---------------------------------------------------------------------------

class RateLimitTracker:
    """Tracks rate-limit events and enforces minimum delay between requests.

    Uses the same Google PSI endpoint as cwv-agent/src/tools/psi.js:
      PSI_URL + GOOGLE_PAGESPEED_INSIGHTS_API_KEY
    """

    def __init__(self, min_delay: float = 0.0, backend: str = "google"):
        self.min_delay = min_delay  # seconds between requests
        self.backend = backend      # "google" (default) or "adobe"
        self.events: List[float] = []  # timestamps of 429 events
        self._last_call: float = 0.0

    def wait(self) -> None:
        """Enforce minimum delay since last call."""
        if self.min_delay > 0:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed)

    def call_psi(
        self,
        url: str,
        strategy: str,
        api_key: Optional[str] = None,
        max_retries: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """Call PSI with retry on rate limit. Records 429 events."""
        import requests as _req

        self.wait()
        if self.backend == "adobe":
            base_url = PSI_ADOBE_URL
            headers = {"User-Agent": "Spacecat/1.0"}
            key = None
        else:  # google (default — matches cwv-agent)
            base_url = PSI_URL
            headers = {}
            key = api_key or PSI_API_KEY  # GOOGLE_PAGESPEED_INSIGHTS_API_KEY

        for attempt in range(max_retries):
            params = [("url", url), ("strategy", strategy)]
            for cat in ["performance", "best-practices", "accessibility", "seo"]:
                params.append(("category", cat))
            if key:
                params.append(("key", key))

            try:
                r = _req.get(base_url, params=params, headers=headers, timeout=120)
                self._last_call = time.monotonic()
                if r.status_code == 429:
                    self.events.append(time.time())
                    retry_after = int(r.headers.get("Retry-After", 60))
                    print(f"  [rate-limit] 429 received — waiting {retry_after}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(retry_after)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:
                self._last_call = time.monotonic()
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    return None
        return None

    def summary(self) -> Dict[str, Any]:
        return {
            "total_429_events": len(self.events),
            "min_delay_configured_s": self.min_delay,
        }

# ---------------------------------------------------------------------------
# Per-sample timing
# ---------------------------------------------------------------------------

class SampleTimer:
    """Records wall-clock time for each phase of a sample."""

    def __init__(self, repo_id: str):
        self.repo_id = repo_id
        self._marks: Dict[str, float] = {}
        self._start = time.monotonic()
        self.mark("start")

    def mark(self, phase: str) -> None:
        self._marks[phase] = time.monotonic()

    def elapsed_since(self, phase: str) -> float:
        return time.monotonic() - self._marks.get(phase, self._start)

    def total(self) -> float:
        return self.elapsed_since("start")

    def phase_durations(self) -> Dict[str, float]:
        phases = list(self._marks.keys())
        durations = {}
        for i in range(1, len(phases)):
            p0, p1 = phases[i - 1], phases[i]
            durations[f"{p0}_to_{p1}"] = round(self._marks[p1] - self._marks[p0], 2)
        durations["total"] = round(self.total(), 2)
        return durations

# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------

def print_timing_summary(timing_records: List[Dict]) -> None:
    if not timing_records:
        return
    totals = [r["total"] for r in timing_records if "total" in r]
    if not totals:
        return
    totals_sorted = sorted(totals)
    p95 = quantiles(totals_sorted, n=20)[18] if len(totals_sorted) >= 20 else totals_sorted[-1]
    print("\n" + "=" * 60)
    print("TIMING SUMMARY (seconds per sample, deploy→tunnel→PSI→teardown)")
    print(f"  Samples       : {len(totals)}")
    print(f"  Avg (mean)    : {mean(totals):.1f}s")
    print(f"  Median (p50)  : {median(totals):.1f}s")
    print(f"  p95           : {p95:.1f}s")
    print(f"  Min           : {min(totals):.1f}s")
    print(f"  Max           : {max(totals):.1f}s")
    print("=" * 60)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk PSI run with tunneling on cwv-bench-v0"
    )
    p.add_argument("--limit",       type=int,   default=300,   help="Number of samples to process (default: 300)")
    p.add_argument("--offset",      type=int,   default=0,     help="Dataset offset to start from (default: 0)")
    p.add_argument("--strategy",    choices=["mobile", "desktop"], default="mobile")
    p.add_argument("--api-key",     default=os.getenv("PSI_API_KEY"), help="Google PSI API key (only needed with --psi-backend google)")
    p.add_argument("--psi-backend", choices=["google", "adobe"], default="google",
                   help="PSI endpoint: 'google' (default, same as cwv-agent, needs GOOGLE_PAGESPEED_INSIGHTS_API_KEY); 'adobe' = internal service")
    p.add_argument("--delay",       type=float, default=0.5,   help="Min delay between PSI calls in seconds (default: 0.5)")
    p.add_argument("--out",         default="bulk_psi_results.jsonl", help="Output JSONL file")
    p.add_argument("--base-dir",    default=None, help="Project root (auto-detected if omitted)")
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


def main() -> None:
    args = parse_args()

    # Resolve project root relative to this file:
    # this file: .../web-experience-benchmark/scripts/helper_scripts/bulk_psi_run_tunneling.py
    #   parents[0] = helper_scripts
    #   parents[1] = scripts
    #   parents[2] = web-experience-benchmark  ← project root
    BASE_DIR = Path(args.base_dir) if args.base_dir else Path(__file__).resolve().parents[2]
    SNAPSHOTS_DIR = BASE_DIR / "harness" / "REPO_SNAPSHOTS"
    WORKSPACE_DIR = BASE_DIR / "tmp_bulk_runs"
    LOG_DIR = BASE_DIR / "logs" / "bulk_psi"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = get_logger("bulk_psi", LOG_DIR / f"run_{ts}.log")
    # If user didn't provide --out, write JSONL next to the log so everything is in one place
    out_path = Path(args.out) if args.out != "bulk_psi_results.jsonl" else LOG_DIR / f"results_{ts}.jsonl"
    logger.info(f"Bulk PSI run starting — limit={args.limit} offset={args.offset} strategy={args.strategy} delay={args.delay}s backend={args.psi_backend} tunnel={args.tunnel_provider}")
    logger.info(f"Log  : {LOG_DIR}/run_{ts}.log")
    logger.info(f"JSONL: {out_path}")

    # Load dataset
    logger.info("Loading dataset …")
    from datasets import load_dataset
    dataset = load_dataset("behavior-in-the-wild/cwv-bench-v0", split="train")
    end_idx = min(args.offset + args.limit, len(dataset))
    samples = dataset.select(range(args.offset, end_idx))
    logger.info(f"Selected {len(samples)} samples (rows {args.offset}–{end_idx - 1})")

    rate_limiter = RateLimitTracker(min_delay=args.delay, backend=args.psi_backend)
    timing_records: List[Dict] = []
    success_count = 0
    skip_count = 0

    out_path = Path(args.out)
    results_file = open(out_path, "a", encoding="utf-8")

    try:
        for i, row in enumerate(tqdm(samples, desc="PSI")):
            repo_id = row.get("REPO_ID", "")
            source_url = row.get("URL", "")
            sample_idx = args.offset + i
            logger.info(f"--- [{sample_idx}] {repo_id} ---")

            timer = SampleTimer(repo_id)

            # ── 1. Repo setup ─────────────────────────────────────────────
            repo_path = setup_repo(repo_id, SNAPSHOTS_DIR, WORKSPACE_DIR, logger)
            if not repo_path:
                logger.warning(f"Skipping {repo_id} — setup failed")
                skip_count += 1
                continue
            timer.mark("repo_ready")

            # ── 2. Framework & commands ───────────────────────────────────
            framework = detect_framework(repo_path)
            port = find_available_port(8000 + (i % 100))
            commands = get_deploy_commands(framework, repo_path, port)
            logger.info(f"Framework: {framework} | Port: {port}")

            # ── 3. Install deps ───────────────────────────────────────────
            ok, err = run_install_commands(commands, repo_path, logger)
            if not ok:
                logger.error(f"Install failed: {err}")
                skip_count += 1
                continue
            timer.mark("install_done")

            # ── 4. Start server ───────────────────────────────────────────
            server_proc = start_server(commands[-1], repo_path, port, logger)
            if not server_proc:
                logger.error("Server failed to start")
                skip_count += 1
                continue
            timer.mark("server_ready")

            # ── 5. Tunnel ─────────────────────────────────────────────────
            tunnel = open_tunnel(args.tunnel_provider, port=port, logger=logger)
            tunnel_url = tunnel.start()
            timer.mark("tunnel_ready")

            if not tunnel_url:
                logger.error("Tunnel failed — skipping PSI")
                stop_server(server_proc, logger)
                skip_count += 1
                continue

            # ── 6. PSI ────────────────────────────────────────────────────
            logger.info(f"PSI on {tunnel_url}")
            psi_raw = rate_limiter.call_psi(tunnel_url, args.strategy, api_key=args.api_key)
            timer.mark("psi_done")

            # ── 7. Cleanup ────────────────────────────────────────────────
            tunnel.stop()
            stop_server(server_proc, logger)
            timer.mark("cleanup_done")

            # ── 8. Record ─────────────────────────────────────────────────
            phase_times = timer.phase_durations()
            timing_records.append(phase_times)

            if psi_raw:
                metrics = extract_metrics(psi_raw)
                result = {
                    "REPO_ID":    repo_id,
                    "SOURCE_URL": source_url,
                    "TUNNEL_URL": tunnel_url,
                    "STRATEGY":   args.strategy,
                    "TIMESTAMP":  datetime.now(timezone.utc).isoformat(),
                    "SAMPLE_IDX": sample_idx,
                    "FRAMEWORK":  framework,
                    "METRICS":    metrics,
                    "TIMING_S":   phase_times,
                }
                results_file.write(json.dumps(result) + "\n")
                results_file.flush()
                success_count += 1
                logger.info(
                    f"  ✓ Done | total={phase_times['total']:.1f}s "
                    f"| perf={metrics['scores'].get('performance', '?')}"
                )
            else:
                logger.error(f"PSI returned no data for {repo_id}")
                skip_count += 1

    finally:
        results_file.close()

    # ── Final summary ──────────────────────────────────────────────────────
    logger.info(f"\nBulk PSI run complete: {success_count} succeeded, {skip_count} skipped")
    rl = rate_limiter.summary()
    logger.info(f"Rate limit events: {rl['total_429_events']} (delay configured: {rl['min_delay_configured_s']}s)")
    print_timing_summary(timing_records)
    logger.info(f"Results: {out_path.resolve()}")


if __name__ == "__main__":
    main()
