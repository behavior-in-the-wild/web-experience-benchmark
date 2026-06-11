#!/usr/bin/env python3
"""
measure_baseline_cwv.py
=======================
Reads input_diverse_100.csv, measures baseline (unpatched) Core Web Vitals
for every row (mobile + desktop), and writes input_100.csv with only the
columns required by evaluate.sh.

Output columns:
  ID, REPO_ID, FRAMEWORK, COMMIT_ID, ZIP_REPO_PATH, HOST_FILE_PATH,
  CWV_MOBILE, CWV_DESKTOP,
  LCP_ENTRIES_MOBILE, LCP_ENTRIES_DESKTOP,
  CLS_SHIFTS_MOBILE, CLS_SHIFTS_DESKTOP,
  INP_INTERACTIONS_MOBILE, INP_INTERACTIONS_DESKTOP

Usage:
  python3 measure_baseline_cwv.py [--workers N] [--num-runs N] [--limit N]
                                   [--resume] [--input PATH] [--output PATH]
"""

import argparse
import csv
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).resolve().parent          # harness/
CWV_SCRIPT   = (SCRIPT_DIR / "../src/cwv_tool/cwv_benchmark.py").resolve()
DEFAULT_IN   = SCRIPT_DIR / "SAMPLE" / "input_diverse_100.csv"
DEFAULT_OUT  = SCRIPT_DIR / "SAMPLE" / "input_100.csv"
CHECKPOINT   = SCRIPT_DIR / "SAMPLE" / "input_100_checkpoint.jsonl"

OUTPUT_COLS = [
    "ID", "REPO_ID", "FRAMEWORK", "COMMIT_ID", "ZIP_REPO_PATH", "HOST_FILE_PATH",
    "CWV_MOBILE", "CWV_DESKTOP",
    "LCP_ENTRIES_MOBILE", "LCP_ENTRIES_DESKTOP",
    "CLS_SHIFTS_MOBILE",  "CLS_SHIFTS_DESKTOP",
    "INP_INTERACTIONS_MOBILE", "INP_INTERACTIONS_DESKTOP",
]

NUM_RUNS     = 5
DEFAULT_WORKERS = 4
BASE_PORT    = 7000

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(SCRIPT_DIR / "SAMPLE" / "measure_baseline.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Port pool
# ---------------------------------------------------------------------------
_port_lock  = threading.Lock()
_used_ports: set[int] = set()

def _alloc_port() -> int:
    with _port_lock:
        p = BASE_PORT
        while p in _used_ports:
            p += 1
        _used_ports.add(p)
        return p

def _free_port(p: int) -> None:
    with _port_lock:
        _used_ports.discard(p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _kill_port(port: int) -> None:
    """Kill all processes listening on *port* (Linux only, lsof fallback)."""
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if out:
            pids = out.split()
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


def _wait_for_server(port: int, timeout: int = 90) -> bool:
    """Poll localhost:port until it responds or timeout expires."""
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{port}/", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def _run_cwv(url: str, device: str, num_runs: int) -> dict:
    """
    Run cwv_benchmark.py --url for a single URL/device combination.
    Returns the parsed JSON dict (may contain status="error").
    """
    cmd = [
        sys.executable, str(CWV_SCRIPT),
        "--url", url,
        "--device", device,
        "--num-runs", str(num_runs),
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,          # 10 min per device run
        )
        raw = result.stdout.decode(errors="replace").strip()
        # Strip any leading log lines before the JSON object
        start = raw.find("{")
        if start > 0:
            raw = raw[start:]
        if raw:
            return json.loads(raw)
        return {"status": "error", "error": "empty output", "runs": [], "aggregated": {},
                "lcp_element": [], "cls_shifts": [], "inp_interactions": []}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "timeout", "runs": [], "aggregated": {},
                "lcp_element": [], "cls_shifts": [], "inp_interactions": []}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"json parse: {e}", "runs": [], "aggregated": {},
                "lcp_element": [], "cls_shifts": [], "inp_interactions": []}
    except Exception as e:
        return {"status": "error", "error": str(e), "runs": [], "aggregated": {},
                "lcp_element": [], "cls_shifts": [], "inp_interactions": []}


# ---------------------------------------------------------------------------
# Per-row measurement
# ---------------------------------------------------------------------------
def measure_row(row: dict, num_runs: int) -> dict:
    """
    Clone → checkout → serve → measure CWV (mobile + desktop) → teardown.
    Returns a dict with all OUTPUT_COLS populated.
    """
    row_id   = row.get("ID", "?")
    repo_id  = row.get("REPO_ID", "")
    commit   = (row.get("COMMIT_ID") or "").strip()
    host_rel = row.get("HOST_FILE_PATH", "").strip()

    host_abs = str(SCRIPT_DIR / host_rel) if host_rel else ""
    port     = _alloc_port()
    work_dir = Path(tempfile.mkdtemp(prefix=f"cwv_baseline_{row_id}_"))
    repo_dir = work_dir / "repo"
    log_file = work_dir / "host.log"

    mobile_json  = {}
    desktop_json = {}

    try:
        logger.info("[%s] Cloning %s ...", row_id, repo_id)
        clone_res = subprocess.run(
            ["git", "clone", f"https://github.com/{repo_id}.git", str(repo_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
        if clone_res.returncode != 0:
            logger.warning("[%s] Clone failed — skipping", row_id)
            return _error_row(row, "clone_failed")

        if commit and commit not in ("null", " "):
            checkout_res = subprocess.run(
                ["git", "-C", str(repo_dir), "checkout", commit],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
            if checkout_res.returncode != 0:
                logger.warning("[%s] Checkout %s failed — using HEAD", row_id, commit[:8])

        # Start HTTP server via the framework-specific host script
        if host_abs and os.path.isfile(host_abs):
            env = {**os.environ, "PORT": str(port)}
            subprocess.Popen(
                ["bash", host_abs, str(repo_dir), str(log_file)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Fallback: plain Python HTTP server
            subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port)],
                cwd=str(repo_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        if not _wait_for_server(port, timeout=90):
            logger.warning("[%s] Server never became ready on port %d", row_id, port)
            return _error_row(row, "server_timeout")

        url = f"http://localhost:{port}"

        logger.info("[%s] Measuring CWV (mobile, %d runs) ...", row_id, num_runs)
        mobile_json = _run_cwv(url, "mobile", num_runs)

        logger.info("[%s] Measuring CWV (desktop, %d runs) ...", row_id, num_runs)
        desktop_json = _run_cwv(url, "desktop", num_runs)

        logger.info("[%s] Done. mobile=%s desktop=%s",
                    row_id,
                    mobile_json.get("status"),
                    desktop_json.get("status"))

    finally:
        _kill_port(port)
        _free_port(port)
        shutil.rmtree(work_dir, ignore_errors=True)

    return _build_output_row(row, mobile_json, desktop_json)


def _json_str(obj) -> str:
    """Serialize an object to a compact JSON string, or empty string on error."""
    if obj is None:
        return ""
    try:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return ""


def _build_output_row(row: dict, mobile: dict, desktop: dict) -> dict:
    # CWV_MOBILE/DESKTOP store only the flat aggregated stats dict (matches input.csv format).
    # If measurement failed, fall back to the full object so the error is still readable.
    def _cwv_cell(obj: dict) -> str:
        agg = obj.get("aggregated")
        if agg and isinstance(agg, dict):
            return _json_str(agg)
        return _json_str(obj)

    return {
        "ID":                       row.get("ID", ""),
        "REPO_ID":                  row.get("REPO_ID", ""),
        "FRAMEWORK":                row.get("FRAMEWORK", ""),
        "COMMIT_ID":                row.get("COMMIT_ID", ""),
        "ZIP_REPO_PATH":            row.get("ZIP_REPO_PATH", ""),
        "HOST_FILE_PATH":           row.get("HOST_FILE_PATH", ""),
        "CWV_MOBILE":               _cwv_cell(mobile),
        "CWV_DESKTOP":              _cwv_cell(desktop),
        "LCP_ENTRIES_MOBILE":       _json_str(mobile.get("lcp_element")),
        "LCP_ENTRIES_DESKTOP":      _json_str(desktop.get("lcp_element")),
        "CLS_SHIFTS_MOBILE":        _json_str(mobile.get("cls_shifts")),
        "CLS_SHIFTS_DESKTOP":       _json_str(desktop.get("cls_shifts")),
        "INP_INTERACTIONS_MOBILE":  _json_str(mobile.get("inp_interactions")),
        "INP_INTERACTIONS_DESKTOP": _json_str(desktop.get("inp_interactions")),
    }


def _error_row(row: dict, reason: str) -> dict:
    err = {"status": "error", "error": reason, "runs": [], "aggregated": {},
           "lcp_element": [], "cls_shifts": [], "inp_interactions": []}
    return _build_output_row(row, err, err)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------
_checkpoint_lock = threading.Lock()

def _load_checkpoint(path: Path) -> dict[str, dict]:
    """Returns {row_id: output_row} from an existing checkpoint file."""
    done: dict[str, dict] = {}
    if not path.exists():
        return done
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    done[str(rec["ID"])] = rec
                except Exception:
                    pass
    return done


def _append_checkpoint(path: Path, rec: dict) -> None:
    with _checkpoint_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Measure baseline CWV for input_diverse_100.csv")
    parser.add_argument("--input",     default=str(DEFAULT_IN),  help="Input CSV path")
    parser.add_argument("--output",    default=str(DEFAULT_OUT), help="Output CSV path")
    parser.add_argument("--workers",   type=int, default=DEFAULT_WORKERS, help="Parallel workers")
    parser.add_argument("--num-runs",  type=int, default=NUM_RUNS, help="CWV runs per device")
    parser.add_argument("--limit",     type=int, default=0,   help="Process only first N rows (0 = all)")
    parser.add_argument("--resume",    action="store_true",   help="Skip rows already in checkpoint")
    args = parser.parse_args()

    if not CWV_SCRIPT.exists():
        logger.error("cwv_benchmark.py not found: %s", CWV_SCRIPT)
        return 1

    csv.field_size_limit(sys.maxsize)
    with open(args.input, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("ID", "").strip()]

    if args.limit > 0:
        rows = rows[:args.limit]

    checkpoint_path = Path(args.output).with_suffix(".checkpoint.jsonl")
    done_map = {}
    if args.resume:
        done_map = _load_checkpoint(checkpoint_path)
        logger.info("Resume: %d rows already done", len(done_map))

    pending = [r for r in rows if str(r["ID"]) not in done_map]
    logger.info("Total rows: %d  |  pending: %d  |  workers: %d  |  num-runs: %d",
                len(rows), len(pending), args.workers, args.num_runs)

    results: dict[str, dict] = dict(done_map)
    t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(measure_row, row, args.num_runs): row for row in pending}
        done_count = len(done_map)
        total = len(rows)
        for fut in as_completed(futures):
            row = futures[fut]
            row_id = str(row["ID"])
            try:
                rec = fut.result()
            except Exception as e:
                logger.error("[%s] Unhandled exception: %s", row_id, e)
                rec = _error_row(row, str(e))
            results[row_id] = rec
            _append_checkpoint(checkpoint_path, rec)
            done_count += 1
            elapsed = time.monotonic() - t0
            rate = done_count / elapsed if elapsed > 0 else 0
            remaining = total - done_count
            eta = remaining / rate if rate > 0 else 0
            logger.info("Progress: %d/%d | %.1f/min | ETA ~%.0f min",
                        done_count, total, rate * 60, eta / 60)

    # Write output CSV in original row order
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLS)
        writer.writeheader()
        for row in rows:
            rid = str(row["ID"])
            if rid in results:
                writer.writerow(results[rid])
            else:
                # Should not happen, but write an error placeholder
                writer.writerow(_error_row(row, "missing"))

    logger.info("Wrote %d rows to %s", len(rows), out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
