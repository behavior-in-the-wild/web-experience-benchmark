#!/usr/bin/env python3
"""
Robust per-repo baseline + per-patch PSI runner using Bore tunnels and checkpointing.

PARALLELISM (v3)
----------------
Added --workers N  (default 1, fully backward-compatible).

Design summary
~~~~~~~~~~~~~~
* ThreadPoolExecutor: workers share in-process state (rate limiter, checkpoint,
  output handles) without IPC overhead.  The bottleneck is I/O (npm install,
  dev-server start, PSI HTTP round-trips), so the GIL is not a bottleneck.

Locks added
~~~~~~~~~~~
_OUTPUT_LOCK      – serialises all appends to JSONL / CSV / MD and all
                    checkpoint mutations.  One writer at a time; writes are
                    fast so contention is minimal.

_RATE_LOCK        – guards the *scheduling* phase of every PSI call: the
                    "wait for pacing" + "update _last_call_mono / _next_allowed"
                    block runs under this lock so workers don't race on the
                    shared RateLimitTracker state.  The actual HTTP request is
                    made *outside* the lock so workers do not serialize on
                    network I/O.

_PORT_LOCK        – makes find_available_port() + reservation atomic so two
                    workers cannot bind the same port.

Per-repo isolation
~~~~~~~~~~~~~~~~~~
* Every repo is cloned into its own UUID-suffixed directory (already the case).
* Every repo measurement uses its own BoreTunnel instance (never shared across
  repos or workers).
* Server log files are keyed by port so they never collide.

Behaviour with --workers 1
~~~~~~~~~~~~~~~~~~~~~~~~~~
Identical to the original: the executor runs tasks sequentially in the calling
thread's pool, all locks are uncontested.

FIXES vs previous version
--------------------------
1. Bore endpoint parsing:
   - ONLY accepts "listening at <host>:<port>" or "remote_port=<port>" lines.
   - Never attempts to parse arbitrary host:port from full log lines.
   - Produces exactly http://bore.pub:PORT.

2. Hung-after-bore-start:
   - probe_public_url logs every single attempt with elapsed time.
   - BoreTunnel drains stdout in a background thread so bore never blocks on write.

3. PSI pacing and retries:
   - Logs "→ PSI request …" BEFORE the call with URL and strategy.
   - Logs "← PSI response …" AFTER with status code and elapsed time.
   - Adaptive global pacing (floor 8 s for Google).
   - Honors Retry-After header precisely.
   - Exponential backoff with full jitter.
   - Classifies errors: invalid_tunnel | unreachable_tunnel | psi_timeout |
     http_429 | http_5xx | parse_failure.

4. Strict cleanup:
   - Every failure path calls _cleanup(server_proc, tunnel, repo_path).
   - _cleanup is idempotent and called in the finally block.

5. Visible per-stage logging:
   - [INSTALL] [SERVER] [BORE] [PROBE] [PSI] [BACKOFF] [CLEANUP] prefixes on
     all key log lines.

6. All existing features preserved:
   - Checkpointing (baseline + per-patch, independently).
   - JSONL + summary CSV + Markdown report incremental append.
   - Patch application with git/patch fallbacks.
   - Repo clone-once-per-repo, reset before each measurement.
   - --fresh, --offset, --limit, --reuse-tunnel-per-repo, --save-cloned-repos.

Usage
-----
# Serial (unchanged behaviour)
python bulk_patch_psi_bore_fixed.py \\
    --input-csv input_psi.csv \\
    --patches-root cwv-agent-v2_patches/cwv-agent-v2_patches \\
    --strategy mobile --psi-backend google --delay 10 \\
    --out patched_bore_psi_results.jsonl \\
    --base-dir "$(pwd)" --limit 999999 --fresh

# Parallel – 4 repos concurrently
python bulk_patch_psi_bore_fixed.py \\
    --input-csv input_psi.csv \\
    --patches-root cwv-agent-v2_patches/cwv-agent-v2_patches \\
    --strategy mobile --psi-backend google --delay 10 \\
    --out patched_bore_psi_results.jsonl \\
    --base-dir "$(pwd)" --limit 999999 --fresh \\
    --workers 4
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, quantiles
from typing import Any, Dict, List, Optional, Tuple

from tqdm import tqdm

from psi_common import (
    PSI_ADOBE_URL,
    PSI_API_KEY,
    PSI_URL,
    detect_framework,
    extract_metrics,
    find_available_port,
    get_deploy_commands,
    get_logger,
)

INSTALL_TIMEOUT = 300        # seconds – generous for large npm installs
SERVER_HEALTH_TIMEOUT = 45   # seconds – allow for Next.js / Nuxt build on first serve
BORE_START_TIMEOUT = 30      # seconds – time to see "listening at …" in bore stdout
PUBLIC_PROBE_TIMEOUT = 40    # seconds – total time budget to probe the public URL
PUBLIC_PROBE_INTERVAL = 2.0  # seconds between probe attempts
TEXT_SIZE_LIMIT = 10 * 1024 * 1024
CHECKPOINT_VERSION = 2

# PSI error classification tags
ERR_INVALID_TUNNEL   = "invalid_tunnel"
ERR_UNREACHABLE      = "unreachable_tunnel"
ERR_PSI_TIMEOUT      = "psi_timeout"
ERR_HTTP_429         = "http_429"
ERR_HTTP_5XX         = "http_5xx"
ERR_PARSE_FAILURE    = "parse_failure"
ERR_UNEXPECTED       = "unexpected"

# ---------------------------------------------------------------------------
# Module-level concurrency primitives
# ---------------------------------------------------------------------------
# All three locks are module-level so they are shared across every object
# instance and every worker thread that imports this module.

_OUTPUT_LOCK = threading.Lock()
"""Serialises writes to JSONL / CSV / Markdown and all checkpoint mutations."""

_RATE_LOCK = threading.Lock()
"""Guards the scheduling / state-update phase of every PSI call."""

_PORT_LOCK = threading.Lock()
"""Makes port probing + reservation atomic so workers never bind the same port."""

# Track reserved ports so find_safe_port() never hands the same one to two workers.
_RESERVED_PORTS: set = set()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def is_valid_public_http_url(url: str) -> bool:
    """Return True only for http(s)://host:port URLs that aren't localhost."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.hostname:
            return False
        if parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
            return False
        if parsed.port is None:
            return False
        if not (1 <= parsed.port <= 65535):
            return False
        return True
    except Exception:
        return False


def normalize_server_host(server: str) -> str:
    s = server.strip()
    if not s:
        return "bore.pub"
    if "://" in s:
        try:
            parsed = urllib.parse.urlparse(s)
            if parsed.hostname:
                return parsed.hostname
        except Exception:
            pass
    s = s.split("/")[0].strip()
    s = s.split(":")[0].strip()
    return s or "bore.pub"


def find_safe_port(base: int) -> int:
    """
    Thread-safe port reservation.

    Wraps psi_common.find_available_port() under _PORT_LOCK and records
    the chosen port in _RESERVED_PORTS so a second worker racing through
    this function before the first worker's server has actually bound the
    port will not receive the same number.
    """
    with _PORT_LOCK:
        candidate = base
        while True:
            if candidate not in _RESERVED_PORTS:
                # Check at the OS level too
                try:
                    chosen = find_available_port(candidate)
                except Exception:
                    chosen = candidate
                if chosen not in _RESERVED_PORTS:
                    _RESERVED_PORTS.add(chosen)
                    return chosen
            candidate += 1


def release_port(port: int) -> None:
    """Release a previously reserved port so it can be reused."""
    with _PORT_LOCK:
        _RESERVED_PORTS.discard(port)


def probe_public_url(
    url: str,
    timeout_s: int = PUBLIC_PROBE_TIMEOUT,
    interval_s: float = PUBLIC_PROBE_INTERVAL,
    logger=None,
) -> bool:
    """
    Repeatedly attempt GET on *url* until a non-5xx response or timeout.

    Logs every attempt with elapsed time, status code (or exception), and
    explicitly logs success/failure reason.
    """
    import requests

    stage = "[PROBE]"

    if not is_valid_public_http_url(url):
        msg = f"{stage} Rejecting invalid public URL before probe: {url!r}"
        print(msg)
        if logger:
            logger.error(msg)
        return False

    if logger:
        logger.info(f"{stage} Starting public URL probe: {url}  (budget={timeout_s}s, interval={interval_s}s)")

    deadline = time.monotonic() + timeout_s
    attempt = 0
    last_exc: Optional[str] = None

    while time.monotonic() < deadline:
        attempt += 1
        t0 = time.monotonic()
        remaining = deadline - time.monotonic()
        per_req_timeout = min(10, max(2, remaining * 0.4))

        try:
            r = requests.get(
                url,
                timeout=per_req_timeout,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 PSI-Patch-Runner/2.0"},
            )
            elapsed = time.monotonic() - t0
            status = r.status_code
            msg = f"{stage} Attempt {attempt}: status={status}  elapsed={elapsed:.2f}s  url={url}"
            print(msg)
            if logger:
                logger.info(msg)

            if status < 500:
                ok_msg = f"{stage} ✓ Tunnel reachable (status={status}, attempt={attempt})"
                print(ok_msg)
                if logger:
                    logger.info(ok_msg)
                return True

            # 5xx – server-side error; bore may not have fully connected yet
            warn = f"{stage} Got {status} (server error); will retry"
            if logger:
                logger.warning(warn)
            last_exc = f"HTTP {status}"

        except requests.exceptions.Timeout:
            elapsed = time.monotonic() - t0
            last_exc = f"Timeout after {elapsed:.2f}s"
            if logger:
                logger.warning(f"{stage} Attempt {attempt}: {last_exc}  url={url}")

        except requests.exceptions.ConnectionError as exc:
            elapsed = time.monotonic() - t0
            last_exc = f"ConnectionError({exc})"
            if logger:
                logger.warning(f"{stage} Attempt {attempt}: {last_exc}  url={url}")

        except Exception as exc:
            elapsed = time.monotonic() - t0
            last_exc = repr(exc)
            if logger:
                logger.warning(f"{stage} Attempt {attempt}: unexpected error {last_exc}  url={url}")

        sleep_for = min(interval_s, deadline - time.monotonic())
        if sleep_for > 0:
            time.sleep(sleep_for)

    fail_msg = (
        f"{stage} ✗ Public URL not reachable after {attempt} attempts "
        f"({timeout_s}s budget): {url}  last_error={last_exc}"
    )
    print(fail_msg)
    if logger:
        logger.error(fail_msg)
    return False


# ---------------------------------------------------------------------------
# Repo helpers
# ---------------------------------------------------------------------------

def sanitize_repo_id(repo_id: str) -> str:
    return re.sub(r"[^\w\-.]", "_", repo_id)


def is_binary_file(path: Path) -> bool:
    try:
        if not path.is_file():
            return True
        if path.stat().st_size > TEXT_SIZE_LIMIT:
            return True
        with open(path, "rb") as f:
            chunk = f.read(4096)
        return b"\x00" in chunk
    except Exception:
        return True


def normalize_file_to_lf(path: Path) -> bool:
    try:
        if is_binary_file(path):
            return False
        raw = path.read_bytes()
        cooked = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if cooked != raw:
            path.write_bytes(cooked)
            return True
        return False
    except Exception:
        return False


def normalize_repo_line_endings(repo_path: Path, logger) -> int:
    changed = 0
    skip_dirs = {
        ".git", "node_modules", ".next", ".nuxt", "dist", "build",
        ".cache", ".parcel-cache", ".venv", "venv", "__pycache__",
    }
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        root_path = Path(root)
        for name in files:
            p = root_path / name
            if normalize_file_to_lf(p):
                changed += 1
    if logger:
        logger.info(f"Normalized line endings in {changed} files")
    return changed


def clone_repo(
    repo_id: str,
    workspace_dir: Path,
    logger,
    branch: Optional[str] = None,
) -> Optional[Path]:
    safe = sanitize_repo_id(repo_id)
    target = workspace_dir / f"{safe}_{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=False)

    cmd = [
        "git", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
        "clone", "--depth", "1",
    ]
    if branch:
        cmd.extend(["-b", branch, "--single-branch"])
    cmd.extend([f"https://github.com/{repo_id}.git", str(target)])

    logger.info(f"[CLONE] Cloning github.com/{repo_id}" + (f" branch={branch}" if branch else ""))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        logger.error(f"[CLONE] Failed for {repo_id}: {res.stderr[:1000]}")
        return None

    for git_cfg in [
        ["git", "-C", str(target), "config", "core.autocrlf", "false"],
        ["git", "-C", str(target), "config", "core.eol", "lf"],
        ["git", "-C", str(target), "config", "apply.whitespace", "nowarn"],
        ["git", "-C", str(target), "reset", "--hard", "HEAD"],
    ]:
        subprocess.run(git_cfg, capture_output=True, text=True)

    normalize_repo_line_endings(target, logger)
    logger.info(f"[CLONE] Success: {target}")
    return target


def reset_repo_to_base(repo_path: Path, logger) -> bool:
    cmds = [
        ["git", "-C", str(repo_path), "reset", "--hard", "HEAD"],
        ["git", "-C", str(repo_path), "clean", "-fdx"],
    ]
    for cmd in cmds:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"[RESET] Command failed: {' '.join(cmd)} | {res.stderr[:500]}")
            return False
    normalize_repo_line_endings(repo_path, logger)
    logger.info(f"[RESET] Repo clean at HEAD: {repo_path.name}")
    return True


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

REPO_ID_REGEX = re.compile(r"^[^/\s]+/[^/\s]+$")


def load_targets_from_csv(csv_path: Path, logger) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames and "REPO_ID" in reader.fieldnames:
                seen: set = set()
                for row in reader:
                    repo_id = str(row.get("REPO_ID", "")).strip()
                    if not REPO_ID_REGEX.match(repo_id) or repo_id in seen:
                        continue
                    seen.add(repo_id)
                    row_id = str(row.get("ID", "")).strip()
                    url = str(row.get("URL", "")).strip()
                    branch = str(row.get("BRANCH", row.get("Branch", ""))).strip() or None
                    if row_id and not re.match(r"^\d+$", row_id):
                        row_id = ""
                    rows.append({"ID": row_id, "REPO_ID": repo_id, "URL": url, "BRANCH": branch})
                if rows:
                    logger.info(f"Loaded {len(rows)} rows from CSV (DictReader)")
                    return rows
    except Exception as exc:
        logger.warning(f"DictReader parse failed, using regex fallback: {exc}")

    repo_pat = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
    id_pat   = re.compile(r'^\s*"?(?P<id>\d+)\b')
    seen2: set = set()
    fallback: List[Dict[str, Any]] = []
    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = repo_pat.search(line)
            if not m:
                continue
            repo_id = m.group(1).strip()
            if not REPO_ID_REGEX.match(repo_id) or repo_id in seen2:
                continue
            seen2.add(repo_id)
            m_id = id_pat.search(line)
            fallback.append({"ID": m_id.group("id") if m_id else "", "REPO_ID": repo_id, "URL": "", "BRANCH": None})
    logger.info(f"Loaded {len(fallback)} rows from CSV (regex fallback)")
    return fallback


# ---------------------------------------------------------------------------
# Patch-folder matching
# ---------------------------------------------------------------------------

def normalize_name(s: str) -> str:
    s = str(s).strip().lower()
    for old, new in [(".github.io", "hubio"), ("github.io", "hubio"), (".hub.io", "hubio"), ("hub.io", "hubio")]:
        s = s.replace(old, new)
    return re.sub(r"[^a-z0-9]+", "", s)


def patch_dir_key(dirname: str) -> str:
    base = re.sub(r"_\d{8}_\d{6}$", "", dirname)
    return normalize_name(base)


def repo_patch_key(repo_id: str) -> str:
    parts = str(repo_id).split("/")
    return normalize_name(parts[1] if len(parts) == 2 else repo_id)


def build_patch_index(patches_root: Path) -> Dict[str, List[Path]]:
    idx: Dict[str, List[Path]] = {}
    for d in patches_root.iterdir():
        if d.is_dir():
            idx.setdefault(patch_dir_key(d.name), []).append(d)
    return idx


def find_patch_dir_for_repo(repo_id: str, patch_index: Dict[str, List[Path]]) -> Optional[Path]:
    target = repo_patch_key(repo_id)
    if target in patch_index and patch_index[target]:
        return sorted(patch_index[target])[0]
    candidates: List[Tuple[int, Path]] = []
    for key, dirs in patch_index.items():
        if target in key or key in target:
            for d in dirs:
                candidates.append((abs(len(key) - len(target)), d))
    if candidates:
        candidates.sort(key=lambda x: (x[0], str(x[1])))
        return candidates[0][1]
    return None


def list_patch_files(patch_dir: Path) -> List[Path]:
    patches_subdir = patch_dir / "patches"
    if not patches_subdir.exists():
        return []
    return sorted(patches_subdir.glob("*.patch"))


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def normalize_patch_file_to_temp_lf(patch_file: Path) -> Path:
    raw = patch_file.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".patch", prefix="normalized_", delete=False)
    tmp.write(normalized)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


def compact_error_text(*parts: str, limit: int = 1500) -> str:
    joined = " | ".join([p.strip() for p in parts if p and p.strip()])
    return re.sub(r"\s+", " ", joined).strip()[:limit] or "patch failed"


def patch_targets(patch_file: Path) -> List[str]:
    targets: List[str] = []
    try:
        for line in patch_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("+++ b/"):
                targets.append(line[len("+++ b/"):].strip())
    except Exception:
        pass
    return targets


def file_lf_stats(path: Path) -> Dict[str, Any]:
    try:
        data = path.read_bytes()
        return {"exists": True, "size": len(data), "crlf": data.count(b"\r\n"),
                "lf": data.count(b"\n"), "binary": b"\x00" in data[:4096]}
    except Exception as exc:
        return {"exists": False, "error": repr(exc)}


def debug_patch_context(repo_path: Path, patch_file: Path, logger) -> None:
    logger.info(f"[PATCH] file={patch_file}  size={patch_file.stat().st_size if patch_file.exists() else 'missing'}")
    targets = patch_targets(patch_file)
    logger.info(f"[PATCH] targets ({len(targets)}): {targets}")
    for rel in targets[:10]:
        target = repo_path / rel
        logger.info(f"[PATCH] target exists={target.exists()}  stats={file_lf_stats(target) if target.exists() else 'N/A'}")


def apply_patch_with_fallback(repo_path: Path, patch_file: Path, logger) -> Tuple[bool, str]:
    repo_str = str(repo_path)
    tmp_patch = normalize_patch_file_to_temp_lf(patch_file)
    patch_str = str(tmp_patch)
    debug_patch_context(repo_path, patch_file, logger)

    try:
        # 1. git apply --check then apply
        chk = subprocess.run(
            ["git", "-C", repo_str, "apply", "--check", "--verbose",
             "--ignore-space-change", "--ignore-whitespace", "--recount", "--inaccurate-eof", patch_str],
            capture_output=True, text=True,
        )
        if chk.returncode == 0:
            ap = subprocess.run(
                ["git", "-C", repo_str, "apply", "--verbose", "--whitespace=nowarn",
                 "--ignore-space-change", "--ignore-whitespace", "--recount", "--inaccurate-eof", patch_str],
                capture_output=True, text=True,
            )
            if ap.returncode == 0:
                logger.info("[PATCH] Applied via git apply")
                return True, "git_apply"

        # 2. git apply --3way
        ap3 = subprocess.run(
            ["git", "-C", repo_str, "apply", "--3way", "--verbose", "--whitespace=nowarn",
             "--ignore-space-change", "--ignore-whitespace", "--recount", "--inaccurate-eof", patch_str],
            capture_output=True, text=True,
        )
        if ap3.returncode == 0:
            logger.info("[PATCH] Applied via git apply --3way")
            return True, "git_apply_3way"

        # 3. git apply --reject (partial)
        ap_rej = subprocess.run(
            ["git", "-C", repo_str, "apply", "--reject", "--verbose", "--whitespace=nowarn",
             "--ignore-space-change", "--ignore-whitespace", "--recount", "--inaccurate-eof", patch_str],
            capture_output=True, text=True,
        )
        rej_text = (ap_rej.stdout or "") + "\n" + (ap_rej.stderr or "")
        if ap_rej.returncode == 0:
            logger.info("[PATCH] Applied via git apply --reject")
            return True, "git_apply_reject"
        if any(kw in rej_text for kw in ("Applied patch", "applied cleanly", "Hunk #")):
            logger.info("[PATCH] Partial apply via git apply --reject")
            return True, "git_apply_reject_partial"

        # 4. system patch utility
        patch_bin = shutil.which("patch")
        if patch_bin:
            ap2 = subprocess.run(
                [patch_bin, "-p1", "--forward", "-l", "--batch", "--binary", "-i", patch_str],
                cwd=repo_str, capture_output=True,
            )
            stdout = ap2.stdout.decode("utf-8", errors="ignore")
            stderr = ap2.stderr.decode("utf-8", errors="ignore")
            if ap2.returncode == 0:
                logger.info("[PATCH] Applied via patch -p1")
                return True, "patch_p1"
            if "applied" in stdout.lower():
                logger.info("[PATCH] Partial apply via patch -p1")
                return True, "patch_p1_partial"
            return False, compact_error_text(chk.stderr, ap3.stderr, ap_rej.stderr, stderr, stdout)

        return False, compact_error_text(chk.stderr, ap3.stderr, ap_rej.stderr, "patch utility not found")

    finally:
        try:
            tmp_patch.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows / shell command adaptation
# ---------------------------------------------------------------------------

_ENV_ASSIGN_RE = re.compile(r"^(?P<envs>(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)+)(?P<cmd>.+)$")


def adapt_segment_for_windows(segment: str) -> str:
    seg = segment.strip()
    if not seg:
        return seg
    if seg.startswith("python3 "):
        seg = "python " + seg[len("python3 "):]
    m = _ENV_ASSIGN_RE.match(seg)
    if not m:
        return seg
    envs_raw = m.group("envs").strip()
    cmd = m.group("cmd").strip()
    set_parts = [f"set {a.split('=',1)[0]}={a.split('=',1)[1]}" for a in envs_raw.split() if "=" in a]
    return " && ".join(set_parts + [cmd])


def adapt_command_for_platform(cmd: str) -> str:
    if os.name != "nt":
        return cmd
    parts = [p.strip() for p in cmd.split("&&")]
    return " && ".join(adapt_segment_for_windows(p) for p in parts if p.strip())


# ---------------------------------------------------------------------------
# Install / start / stop helpers
# ---------------------------------------------------------------------------

def run_install_commands_compat(commands: List[str], repo_path: Path, logger) -> Tuple[bool, str]:
    for raw_cmd in commands[:-1]:
        cmd = adapt_command_for_platform(raw_cmd)
        logger.info(f"[INSTALL] $ {cmd}")
        t0 = time.monotonic()
        try:
            res = subprocess.run(
                cmd, shell=True, cwd=str(repo_path),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=INSTALL_TIMEOUT, text=True,
            )
            elapsed = time.monotonic() - t0
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "")[:1500]
                logger.error(f"[INSTALL] FAILED ({elapsed:.1f}s): {cmd}  error={err[:300]}")
                return False, f"Command failed [{cmd}]: {err}"
            logger.info(f"[INSTALL] OK ({elapsed:.1f}s): {cmd}")
        except subprocess.TimeoutExpired:
            logger.error(f"[INSTALL] TIMEOUT after {INSTALL_TIMEOUT}s: {cmd}")
            return False, f"Timeout after {INSTALL_TIMEOUT}s: {cmd}"
        except Exception as exc:
            logger.error(f"[INSTALL] ERROR: {cmd}  exc={exc}")
            return False, f"Error running [{cmd}]: {exc}"
    return True, ""


def start_server_compat(serve_cmd: str, repo_path: Path, port: int, logger) -> Optional[subprocess.Popen]:
    cmd = adapt_command_for_platform(serve_cmd)
    log_file = repo_path.parent / f"server_{port}.log"
    logger.info(f"[SERVER] Starting: {cmd}  port={port}")

    log_handle = open(log_file, "w", encoding="utf-8", errors="ignore")
    try:
        kwargs: Dict[str, Any] = dict(
            shell=True, cwd=str(repo_path),
            stdout=log_handle, stderr=subprocess.STDOUT,
        )
        if os.name == "nt":
            proc = subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), **kwargs)
        else:
            proc = subprocess.Popen(cmd, preexec_fn=os.setsid, **kwargs)

        proc._server_log_handle = log_handle   # type: ignore[attr-defined]
        proc._server_log_file   = str(log_file) # type: ignore[attr-defined]
        logger.info(f"[SERVER] PID={proc.pid}  log={log_file}")

        deadline = time.monotonic() + SERVER_HEALTH_TIMEOUT
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            if proc.poll() is not None:
                logger.error(f"[SERVER] Exited early (code={proc.returncode}) on attempt {attempt}")
                return None
            try:
                with urllib.request.urlopen(f"http://localhost:{port}", timeout=3) as r:
                    if getattr(r, "status", 200) < 500:
                        elapsed = SERVER_HEALTH_TIMEOUT - (deadline - time.monotonic())
                        logger.info(f"[SERVER] Healthy at localhost:{port}  attempt={attempt}  elapsed={elapsed:.1f}s")
                        return proc
            except Exception as exc:
                logger.debug(f"[SERVER] Attempt {attempt}: not ready ({exc})")
            time.sleep(1)

        # Still running but slow – give benefit of the doubt
        if proc.poll() is None:
            logger.warning(f"[SERVER] Still running after {SERVER_HEALTH_TIMEOUT}s health timeout – proceeding cautiously")
            return proc

        logger.error(f"[SERVER] Never became healthy within {SERVER_HEALTH_TIMEOUT}s")
        return None

    except Exception as exc:
        logger.error(f"[SERVER] Failed to launch: {exc}")
        try:
            log_handle.close()
        except Exception:
            pass
        return None


def stop_server_compat(proc: Optional[subprocess.Popen], logger) -> None:
    if proc is None:
        return
    pid = proc.pid
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as exc:
        logger.warning(f"[CLEANUP] stop_server error (pid={pid}): {exc}")
    finally:
        try:
            lh = getattr(proc, "_server_log_handle", None)
            if lh:
                lh.close()
        except Exception:
            pass
    logger.info(f"[CLEANUP] Server stopped (pid={pid})")


# ---------------------------------------------------------------------------
# Bore tunnel – fixed endpoint parser + background stdout drain
# ---------------------------------------------------------------------------

@dataclass
class BoreEndpoint:
    host: str
    remote_port: int

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.remote_port}"


class BoreTunnel:
    """
    Wraps the `bore local` subprocess.

    Endpoint detection strategy (strict, in priority order):
      1. "listening at <host>:<port>"  – bore ≥0.5 canonical success line
      2. "remote_port = <N>" or "remote_port=<N>"  – bore structured log

    Nothing else is accepted.  This prevents timestamp fragments, internal
    server addresses, or other numeric tokens from being misidentified.

    A background thread drains stdout so the bore process never blocks
    writing to its pipe.

    Thread-safety: each BoreTunnel instance is owned by exactly one repo
    worker.  No cross-worker sharing; no extra locking needed inside this
    class.
    """

    def __init__(
        self,
        local_port: int,
        logger,
        bore_binary: str = "bore",
        server: str = "bore.pub",
        local_host: str = "localhost",
        secret: Optional[str] = None,
    ):
        self.local_port  = int(local_port)
        self.logger      = logger
        self.bore_binary = bore_binary
        self.server      = server
        self.local_host  = local_host
        self.secret      = secret
        self.proc:     Optional[subprocess.Popen] = None
        self.endpoint: Optional[BoreEndpoint]    = None
        self.log_file: Optional[Path]            = None
        self._drain_thread: Optional[threading.Thread] = None
        self._line_queue:   queue.Queue           = queue.Queue()

    def public_host(self) -> str:
        return normalize_server_host(self.server)

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self, timeout: int = BORE_START_TIMEOUT) -> Optional[str]:
        if self.is_running() and self.endpoint is not None:
            return self.endpoint.http_url

        cmd = [self.bore_binary, "local", str(self.local_port), "--to", self.server,
               "--local-host", self.local_host]
        if self.secret:
            cmd.extend(["--secret", self.secret])

        safe_server = re.sub(r"[^A-Za-z0-9_.-]", "_", self.server)
        self.log_file = Path.cwd() / f"bore_{safe_server}_{self.local_port}_{uuid.uuid4().hex[:6]}.log"
        log_handle    = open(self.log_file, "w", encoding="utf-8", errors="ignore")

        self.logger.info(f"[BORE] Starting: {' '.join(cmd)}")

        try:
            kwargs: Dict[str, Any] = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if os.name == "nt":
                self.proc = subprocess.Popen(
                    cmd,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    **kwargs,
                )
            else:
                self.proc = subprocess.Popen(cmd, preexec_fn=os.setsid, **kwargs)

            assert self.proc.stdout is not None

            # Background thread: drain stdout → queue + log file
            self._line_queue = queue.Queue()
            self._drain_thread = threading.Thread(
                target=self._drain_stdout,
                args=(self.proc.stdout, log_handle, self._line_queue),
                daemon=True,
                name=f"bore-drain-{self.local_port}",
            )
            self._drain_thread.start()

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    self.logger.error(f"[BORE] Process exited early (code={self.proc.returncode})")
                    break
                try:
                    line = self._line_queue.get(timeout=0.3)
                except queue.Empty:
                    continue

                self.logger.info(f"[BORE] {line}")
                ep = self._parse_endpoint_strict(line)
                if ep is not None:
                    self.endpoint = ep
                    url = self.endpoint.http_url
                    self.logger.info(f"[BORE] ✓ Endpoint ready: {url}")
                    print(f"[BORE] Tunnel up: {url}")
                    return url

            # Drain remaining lines for diagnostics
            tail: List[str] = []
            while True:
                try:
                    tail.append(self._line_queue.get_nowait())
                except queue.Empty:
                    break
            self.logger.error(f"[BORE] Failed to parse endpoint within {timeout}s. Last lines: {tail[-8:]}")
            self.stop()
            return None

        except FileNotFoundError:
            self.logger.error(f"[BORE] Binary not found: {self.bore_binary}")
            self.stop()
            return None
        except Exception as exc:
            self.logger.error(f"[BORE] Start failed: {exc}")
            self.stop()
            return None

    def ensure_running(self) -> Optional[str]:
        if self.is_running() and self.endpoint is not None:
            return self.endpoint.http_url
        self.stop()
        return self.start()

    def stop(self) -> None:
        proc = self.proc
        self.proc     = None
        self.endpoint = None

        if proc is not None:
            pid = proc.pid
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    )
                else:
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except Exception as exc:
                self.logger.warning(f"[BORE] Stop error (pid={pid}): {exc}")
            self.logger.info(f"[BORE] Stopped (pid={pid})")

        if self._drain_thread is not None:
            self._drain_thread.join(timeout=3)
            self._drain_thread = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _drain_stdout(
        stdout,
        log_handle,
        line_queue: queue.Queue,
    ) -> None:
        """Read lines from bore stdout; put them in queue and write to log."""
        try:
            for raw_line in stdout:
                line = raw_line.rstrip("\n")
                try:
                    log_handle.write(line + "\n")
                    log_handle.flush()
                except Exception:
                    pass
                line_queue.put(line)
        except Exception:
            pass
        finally:
            try:
                log_handle.close()
            except Exception:
                pass

    def _parse_endpoint_strict(self, line: str) -> Optional[BoreEndpoint]:
        """
        STRICT parser – only two accepted forms:

          Form 1 (bore ≥0.5 canonical):
            … listening at bore.pub:48958 …
            (the host part must match our known public host)

          Form 2 (bore structured log):
            … remote_port = 48958 …
            … remote_port=48958 …

        Anything else is IGNORED.  This is intentional.
        """
        host = self.public_host()
        text = line.strip()

        # Form 1: "listening at <host>:<port>"
        m1 = re.search(
            r"\blistening\s+at\s+([A-Za-z0-9_.-]+):(\d{2,5})\b",
            text,
            re.IGNORECASE,
        )
        if m1:
            parsed_host = m1.group(1)
            port = int(m1.group(2))
            if 1 <= port <= 65535:
                self.logger.info(f"[BORE] Matched Form1 'listening at': host={parsed_host} port={port}")
                return BoreEndpoint(host=parsed_host, remote_port=port)

        # Form 2: "remote_port = <N>" or "remote_port=<N>"
        m2 = re.search(r"\bremote_port\s*[=:]\s*(\d{2,5})\b", text)
        if m2:
            port = int(m2.group(1))
            if 1 <= port <= 65535:
                self.logger.info(f"[BORE] Matched Form2 'remote_port': host={host} port={port}")
                return BoreEndpoint(host=host, remote_port=port)

        return None


# ---------------------------------------------------------------------------
# PSI wrapper – classified errors, per-call logging, adaptive pacing
# ---------------------------------------------------------------------------

class RateLimitTracker:
    """
    Wraps PSI HTTP calls with:
      - Per-call logging (before + after with elapsed time).
      - Adaptive global floor delay (≥8 s for Google).
      - Exponential backoff with full jitter on 429/5xx.
      - Precise Retry-After honoring.
      - Classified error tags (see ERR_* constants).

    Thread-safety (parallel workers)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    The *scheduling* phase (wait for pacing + update timestamps) runs under
    _RATE_LOCK so concurrent workers serialise the "when may I next call PSI?"
    decision.  The actual HTTP request is made OUTSIDE the lock so workers
    can overlap on network I/O once pacing has been satisfied.

    The _note_success / _note_failure methods also acquire _RATE_LOCK because
    they mutate shared timing state (_last_call_mono, _next_allowed_at,
    _adaptive_delay).
    """

    def __init__(self, min_delay: float = 0.0, backend: str = "google", logger=None):
        self.base_min_delay = float(min_delay)
        self.backend        = backend
        self.logger         = logger

        floor = 8.0 if backend == "google" else 0.0
        self._adaptive_delay = max(self.base_min_delay, floor)
        self._last_call_mono: float = 0.0
        self._next_allowed_at: float = 0.0
        self._429_events: List[float] = []
        self.successes = 0
        self.failures  = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call_psi(
        self,
        url: str,
        strategy: str,
        api_key: Optional[str] = None,
        max_retries: int = 5,
        timeout: int = 120,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        Returns (payload_or_None, error_tag).
        error_tag is "" on success, one of ERR_* constants on failure.

        Concurrency note: _RATE_LOCK is acquired only around the pacing wait
        and state updates.  The HTTP round-trip itself is unlocked so multiple
        workers can be in-flight simultaneously (Google PSI allows parallel
        calls; the lock just enforces the minimum inter-call gap globally).
        """
        import requests

        if not is_valid_public_http_url(url):
            self._log(f"[PSI] ✗ Invalid public URL – refusing call: {url!r}", error=True)
            return None, ERR_INVALID_TUNNEL

        if self.backend == "adobe":
            base_url = PSI_ADOBE_URL
            headers  = {"User-Agent": "Spacecat/1.0"}
            key      = None
        else:
            base_url = PSI_URL
            headers  = {}
            key      = api_key or PSI_API_KEY or os.getenv("GOOGLE_PAGESPEED_INSIGHTS_API_KEY")

        if self.backend == "google" and not key:
            self._log("[PSI] ✗ No API key for Google PSI backend", error=True)
            return None, ERR_INVALID_TUNNEL

        last_tag = ERR_UNEXPECTED

        for attempt in range(1, max_retries + 1):
            # ── Pacing: acquire lock, wait, stamp, release ─────────────────
            with _RATE_LOCK:
                self._wait_global_pacing_locked()
                # Stamp the start time while holding the lock so the next
                # worker sees an up-to-date _last_call_mono.
                self._last_call_mono = time.monotonic()

            params = [("url", url), ("strategy", strategy)]
            for cat in ["performance", "best-practices", "accessibility", "seo"]:
                params.append(("category", cat))
            if key:
                params.append(("key", key))

            self._log(f"[PSI] → Attempt {attempt}/{max_retries}  strategy={strategy}  url={url}")

            t0 = time.monotonic()
            try:
                r = requests.get(base_url, params=params, headers=headers, timeout=timeout)
                elapsed = time.monotonic() - t0
                status  = r.status_code
                self._log(f"[PSI] ← status={status}  elapsed={elapsed:.2f}s  attempt={attempt}")

                if status == 429:
                    retry_after = self._parse_retry_after(r.headers.get("Retry-After", ""))
                    if retry_after is None:
                        cap   = min(120.0, 15.0 * (2 ** (attempt - 1)))
                        retry_after = random.uniform(0, cap)
                    cooldown = retry_after + random.uniform(0.5, 2.0)
                    with _RATE_LOCK:
                        self._429_events.append(time.time())
                        self._note_failure_locked(cooldown)
                    last_tag = ERR_HTTP_429
                    self._log(
                        f"[PSI] [BACKOFF] 429 rate-limited – "
                        f"Retry-After={retry_after:.1f}s, sleeping {cooldown:.1f}s "
                        f"(attempt {attempt}/{max_retries})",
                        warning=True,
                    )
                    continue

                if 500 <= status < 600:
                    cap      = min(60.0, 10.0 * (2 ** (attempt - 1)))
                    cooldown = random.uniform(cap * 0.5, cap) + random.uniform(0.5, 2.0)
                    with _RATE_LOCK:
                        self._note_failure_locked(cooldown)
                    last_tag = ERR_HTTP_5XX
                    self._log(
                        f"[PSI] [BACKOFF] HTTP {status} – sleeping {cooldown:.1f}s "
                        f"(attempt {attempt}/{max_retries})",
                        warning=True,
                    )
                    continue

                r.raise_for_status()

                try:
                    payload = r.json()
                except Exception as parse_exc:
                    self._log(f"[PSI] ✗ JSON parse failure: {parse_exc}", error=True)
                    last_tag = ERR_PARSE_FAILURE
                    with _RATE_LOCK:
                        self._note_failure_locked(5.0)
                    continue

                with _RATE_LOCK:
                    self._note_success_locked()
                self._log(f"[PSI] ✓ Success  elapsed={elapsed:.2f}s  attempt={attempt}")
                return payload, ""

            except __import__("requests").exceptions.Timeout:
                elapsed = time.monotonic() - t0
                cap     = min(30.0, 5.0 * attempt)
                cooldown = random.uniform(cap * 0.5, cap)
                with _RATE_LOCK:
                    self._note_failure_locked(cooldown)
                last_tag = ERR_PSI_TIMEOUT
                self._log(
                    f"[PSI] [BACKOFF] Timeout after {elapsed:.2f}s – "
                    f"sleeping {cooldown:.1f}s  (attempt {attempt}/{max_retries})",
                    warning=True,
                )

            except __import__("requests").exceptions.ConnectionError as exc:
                elapsed  = time.monotonic() - t0
                cap      = min(30.0, 5.0 * attempt)
                cooldown = random.uniform(cap * 0.5, cap)
                with _RATE_LOCK:
                    self._note_failure_locked(cooldown)
                last_tag = ERR_UNREACHABLE
                self._log(
                    f"[PSI] [BACKOFF] ConnectionError({exc}) – "
                    f"sleeping {cooldown:.1f}s  (attempt {attempt}/{max_retries})",
                    warning=True,
                )

            except Exception as exc:
                elapsed  = time.monotonic() - t0
                cooldown = min(30.0, 5.0 * attempt)
                with _RATE_LOCK:
                    self._note_failure_locked(cooldown)
                last_tag = ERR_UNEXPECTED
                self._log(
                    f"[PSI] [BACKOFF] Unexpected error: {exc!r} – "
                    f"sleeping {cooldown:.1f}s  (attempt {attempt}/{max_retries})",
                    error=True,
                )

        self._log(f"[PSI] ✗ All {max_retries} attempts exhausted – last_tag={last_tag}", error=True)
        return None, last_tag

    def summary(self) -> Dict[str, Any]:
        with _RATE_LOCK:
            return {
                "total_429_events":           len(self._429_events),
                "configured_min_delay_s":     self.base_min_delay,
                "effective_adaptive_delay_s": round(self._adaptive_delay, 2),
                "successes": self.successes,
                "failures":  self.failures,
            }

    # ------------------------------------------------------------------
    # Internal helpers  (all *_locked variants must be called under _RATE_LOCK)
    # ------------------------------------------------------------------

    def _log(self, msg: str, warning: bool = False, error: bool = False) -> None:
        print(msg)
        if self.logger:
            if error:
                self.logger.error(msg)
            elif warning:
                self.logger.warning(msg)
            else:
                self.logger.info(msg)

    def _wait_global_pacing_locked(self) -> None:
        """Must be called while holding _RATE_LOCK."""
        now = time.monotonic()
        wait = max(0.0, self._next_allowed_at - now)
        elapsed_since_last = now - self._last_call_mono
        if elapsed_since_last < self._adaptive_delay:
            wait = max(wait, self._adaptive_delay - elapsed_since_last)
        if wait > 0:
            self._log(
                f"[PSI] Pacing: waiting {wait:.1f}s before next call "
                f"(adaptive_delay={self._adaptive_delay:.1f}s)"
            )
            # Release lock while sleeping so other workers can check their own
            # pacing without being blocked.
            _RATE_LOCK.release()
            try:
                time.sleep(wait)
            finally:
                _RATE_LOCK.acquire()

    def _note_success_locked(self) -> None:
        """Must be called while holding _RATE_LOCK."""
        self.successes += 1
        self._last_call_mono = time.monotonic()
        floor = max(self.base_min_delay, 8.0 if self.backend == "google" else 0.0)
        self._adaptive_delay = max(floor, self._adaptive_delay * 0.85)

    def _note_failure_locked(self, cooldown_s: float) -> None:
        """Must be called while holding _RATE_LOCK."""
        self.failures += 1
        now = time.monotonic()
        self._last_call_mono  = now
        self._next_allowed_at = max(self._next_allowed_at, now + cooldown_s)
        self._adaptive_delay  = min(max(self._adaptive_delay * 1.5, cooldown_s), 180.0)
        self._log(
            f"[PSI] [BACKOFF] next_allowed_in={cooldown_s:.1f}s  "
            f"adaptive_delay={self._adaptive_delay:.1f}s"
        )

    @staticmethod
    def _parse_retry_after(header_val: str) -> Optional[float]:
        """Parse Retry-After header; return seconds as float, or None."""
        val = header_val.strip()
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(val)
            secs = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, secs)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Timing / output helpers
# ---------------------------------------------------------------------------

class SampleTimer:
    def __init__(self, sample_name: str):
        self.sample_name = sample_name
        self._marks: Dict[str, float] = {}
        self._start = time.monotonic()
        self.mark("start")

    def mark(self, phase: str) -> None:
        self._marks[phase] = time.monotonic()

    def total(self) -> float:
        return time.monotonic() - self._start

    def phase_durations(self) -> Dict[str, float]:
        phases = list(self._marks.keys())
        out: Dict[str, float] = {}
        for i in range(1, len(phases)):
            p0, p1 = phases[i - 1], phases[i]
            out[f"{p0}_to_{p1}"] = round(self._marks[p1] - self._marks[p0], 2)
        out["total"] = round(self.total(), 2)
        return out


def flatten_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    scores = metrics.get("scores", {})
    nums   = metrics.get("metrics", {})
    return {
        "score_performance":    scores.get("performance"),
        "score_accessibility":  scores.get("accessibility"),
        "score_best_practices": scores.get("best-practices"),
        "score_seo":            scores.get("seo"),
        "LCP":        nums.get("LCP"),
        "CLS":        nums.get("CLS"),
        "INP":        nums.get("INP"),
        "FCP":        nums.get("FCP"),
        "TTFB":       nums.get("TTFB"),
        "TBT":        nums.get("TBT"),
        "SpeedIndex": nums.get("SpeedIndex"),
    }


def print_timing_summary(timing_records: List[Dict[str, float]]) -> None:
    totals = [r["total"] for r in timing_records if "total" in r]
    if not totals:
        return
    totals_sorted = sorted(totals)
    p95 = quantiles(totals_sorted, n=20)[18] if len(totals_sorted) >= 20 else totals_sorted[-1]
    print("\n" + "=" * 72)
    print("TIMING SUMMARY (seconds per baseline/patch record)")
    print(f"  Records: {len(totals)}")
    print(f"  Mean:    {mean(totals):.1f}s")
    print(f"  Median:  {median(totals):.1f}s")
    print(f"  p95:     {p95:.1f}s")
    print(f"  Min:     {min(totals):.1f}s")
    print(f"  Max:     {max(totals):.1f}s")
    print("=" * 72)


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    """Append one JSON line.  Caller must hold _OUTPUT_LOCK."""
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


SUMMARY_FIELDNAMES = [
    "ID", "REPO_ID", "RECORD_TYPE", "PATCH_FILE", "PATCH_DIR", "FRAMEWORK",
    "STRATEGY", "PSI_BACKEND", "PATCH_APPLY_MODE", "STATUS", "ERROR_STAGE",
    "ERROR", "ERROR_TAG",
    "score_performance", "score_accessibility", "score_best_practices", "score_seo",
    "LCP", "CLS", "INP", "FCP", "TTFB", "TBT", "SpeedIndex",
]


def append_summary_csv(path: Path, row: Dict[str, Any]) -> None:
    """Append one CSV row.  Caller must hold _OUTPUT_LOCK."""
    ensure_parent(path)
    file_exists = path.exists() and path.stat().st_size > 0
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDNAMES, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def append_markdown_report(path: Path, result: Dict[str, Any]) -> None:
    """Append one Markdown section.  Caller must hold _OUTPUT_LOCK."""
    ensure_parent(path)
    first_write = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if first_write:
            f.write("# Baseline + Patch PSI Report\n\n")
        f.write(f"## {result.get('REPO_ID','?')} — {result.get('RECORD_LABEL', result.get('RECORD_TYPE','?'))}\n\n")
        for label, key in [
            ("Status",        "STATUS"),
            ("Timestamp UTC", "TIMESTAMP"),
            ("Record type",   "RECORD_TYPE"),
            ("Patch file",    "PATCH_FILE"),
            ("Framework",     "FRAMEWORK"),
            ("Strategy",      "STRATEGY"),
            ("PSI backend",   "PSI_BACKEND"),
            ("Tunnel URL",    "TUNNEL_URL"),
            ("Patch mode",    "PATCH_APPLY_MODE"),
            ("Error stage",   "ERROR_STAGE"),
            ("Error tag",     "ERROR_TAG"),
            ("Error",         "ERROR"),
        ]:
            val = result.get(key)
            if val is not None:
                f.write(f"- **{label}:** {val}\n")

        flat = result.get("FLAT_METRICS") or {}
        if flat:
            f.write("\n### Metrics\n\n")
            for k in ["score_performance","score_accessibility","score_best_practices","score_seo",
                      "LCP","CLS","INP","FCP","TTFB","TBT","SpeedIndex"]:
                f.write(f"- **{k}:** {flat.get(k)}\n")

        timings = result.get("TIMING_S") or {}
        if timings:
            f.write("\n### Timings (s)\n\n")
            for k, v in timings.items():
                f.write(f"- **{k}:** {v}\n")

        f.write("\n---\n\n")


# ---------------------------------------------------------------------------
# Checkpoint management
# ---------------------------------------------------------------------------

class CheckpointManager:
    """
    Thread-safe checkpoint manager.

    All public methods acquire _OUTPUT_LOCK before reading or mutating state
    so concurrent workers never see a partially-written checkpoint.
    """

    def __init__(self, path: Path, args_dict: Dict[str, Any]):
        self.path      = path
        self.args_dict = args_dict
        self.state     = self._load_or_init()

    def _load_or_init(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if isinstance(state, dict):
                    state.setdefault("version",    CHECKPOINT_VERSION)
                    state.setdefault("created_at", utc_now_iso())
                    state.setdefault("updated_at", utc_now_iso())
                    state.setdefault("args",       self.args_dict)
                    state.setdefault("completed",  {})
                    state.setdefault("stats",      {"ok": 0, "error": 0})
                    return state
            except Exception:
                pass
        return {
            "version":    CHECKPOINT_VERSION,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "args":       self.args_dict,
            "completed":  {},
            "stats":      {"ok": 0, "error": 0},
        }

    def has(self, key: str) -> bool:
        """Check whether a key is already completed.  Thread-safe."""
        with _OUTPUT_LOCK:
            return key in self.state.get("completed", {})

    def mark(self, key: str, result: Dict[str, Any]) -> None:
        """Record a completed measurement.  Thread-safe (acquires _OUTPUT_LOCK)."""
        with _OUTPUT_LOCK:
            self._mark_locked(key, result)

    def _mark_locked(self, key: str, result: Dict[str, Any]) -> None:
        """Must be called while _OUTPUT_LOCK is held."""
        compact = {
            "repo_id":     result.get("REPO_ID"),
            "record_type": result.get("RECORD_TYPE"),
            "patch_file":  result.get("PATCH_FILE"),
            "status":      result.get("STATUS"),
            "timestamp":   result.get("TIMESTAMP"),
            "score_performance": (result.get("FLAT_METRICS") or {}).get("score_performance"),
            "error_stage": result.get("ERROR_STAGE"),
            "error_tag":   result.get("ERROR_TAG"),
            "error":       result.get("ERROR"),
        }
        self.state.setdefault("completed", {})[key] = compact
        stats = self.state.setdefault("stats", {"ok": 0, "error": 0})
        if result.get("STATUS") == "ok":
            stats["ok"]    = int(stats.get("ok",    0)) + 1
        else:
            stats["error"] = int(stats.get("error", 0)) + 1
        self.state["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, self.state)


# ---------------------------------------------------------------------------
# Strict cleanup helper
# ---------------------------------------------------------------------------

def _cleanup(
    server_proc: Optional[subprocess.Popen],
    tunnel: Optional[BoreTunnel],
    logger,
    label: str = "",
) -> None:
    """
    Idempotent cleanup: stop server → stop tunnel.
    Called in every finally block so no resource is ever leaked.
    """
    tag = f"[CLEANUP]{(' ' + label) if label else ''}"
    logger.info(f"{tag} Starting cleanup")

    if server_proc is not None:
        try:
            stop_server_compat(server_proc, logger)
        except Exception as exc:
            logger.warning(f"{tag} Error stopping server: {exc}")

    if tunnel is not None:
        try:
            tunnel.stop()
        except Exception as exc:
            logger.warning(f"{tag} Error stopping tunnel: {exc}")

    logger.info(f"{tag} Cleanup complete")


# ---------------------------------------------------------------------------
# Checkpoint key helpers
# ---------------------------------------------------------------------------

def baseline_key(repo_id: str) -> str:
    return f"{repo_id}::baseline"


def patch_key(repo_id: str, patch_file: str) -> str:
    return f"{repo_id}::patch::{patch_file}"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def make_summary_row(result: Dict[str, Any]) -> Dict[str, Any]:
    flat = result.get("FLAT_METRICS") or {}
    return {
        "ID":               result.get("ID"),
        "REPO_ID":          result.get("REPO_ID"),
        "RECORD_TYPE":      result.get("RECORD_TYPE"),
        "PATCH_FILE":       result.get("PATCH_FILE"),
        "PATCH_DIR":        result.get("PATCH_DIR"),
        "FRAMEWORK":        result.get("FRAMEWORK"),
        "STRATEGY":         result.get("STRATEGY"),
        "PSI_BACKEND":      result.get("PSI_BACKEND"),
        "PATCH_APPLY_MODE": result.get("PATCH_APPLY_MODE"),
        "STATUS":           result.get("STATUS"),
        "ERROR_STAGE":      result.get("ERROR_STAGE"),
        "ERROR":            result.get("ERROR"),
        "ERROR_TAG":        result.get("ERROR_TAG"),
        **flat,
    }


def write_record(
    outputs: Dict[str, Path],
    checkpoint: CheckpointManager,
    checkpoint_key: str,
    result: Dict[str, Any],
) -> None:
    """
    Atomically append to all output files and update the checkpoint.

    Acquires _OUTPUT_LOCK once for the entire operation so the three files
    and the checkpoint JSON are always updated together.
    """
    with _OUTPUT_LOCK:
        append_jsonl(outputs["jsonl"], result)
        append_summary_csv(outputs["csv"], make_summary_row(result))
        append_markdown_report(outputs["md"], result)
        # Checkpoint mark also needs the lock; call the internal variant
        # directly since we already hold it.
        checkpoint._mark_locked(checkpoint_key, result)


# ---------------------------------------------------------------------------
# Core measurement function
# ---------------------------------------------------------------------------

def deploy_and_measure(
    *,
    repo_path: Path,
    repo_id: str,
    sample_id: str,
    source_url: str,
    sample_idx: int,
    record_type: str,
    patch_idx: Optional[int],
    patch_file: Optional[Path],
    patch_dir: Optional[Path],
    args: argparse.Namespace,
    logger,
    rate_limiter: RateLimitTracker,
    port: int,
    shared_tunnel: Optional[BoreTunnel],
) -> Dict[str, Any]:
    """
    Install → start server → start/reuse tunnel → probe public URL → call PSI.

    Every early-return path calls _cleanup() before returning so resources
    are always released.  The tunnel passed in as shared_tunnel is NOT stopped
    here (caller owns it); only a tunnel we create ourselves is stopped.
    """
    timer = SampleTimer(f"{repo_id}:{record_type}:{patch_file.name if patch_file else 'baseline'}")
    label = f"{repo_id}::{record_type}::{patch_file.name if patch_file else 'baseline'}"

    server_proc: Optional[subprocess.Popen]  = None
    own_tunnel:  Optional[BoreTunnel]        = None   # tunnel we own and must stop

    result: Dict[str, Any] = {
        "REPO_ID":       repo_id,
        "SOURCE_URL":    source_url,
        "ID":            sample_id,
        "SAMPLE_IDX":    sample_idx,
        "PATCH_IDX":     patch_idx,
        "PATCH_FILE":    patch_file.name if patch_file else None,
        "PATCH_PATH":    str(patch_file) if patch_file else None,
        "PATCH_DIR":     str(patch_dir)  if patch_dir  else None,
        "PATCH_KEY":     repo_patch_key(repo_id),
        "RECORD_TYPE":   record_type,
        "RECORD_LABEL":  "baseline" if record_type == "baseline" else f"patch:{patch_file.name}",
        "STRATEGY":      args.strategy,
        "TIMESTAMP":     utc_now_iso(),
        "PSI_BACKEND":   args.psi_backend,
        "STATUS":        "error",
        "PORT":          port,
    }

    try:
        # ── 1. Detect framework + build commands ───────────────────────────
        logger.info(f"[MEASURE] {label}: detecting framework")
        framework    = detect_framework(repo_path)
        raw_commands = get_deploy_commands(framework, repo_path, port)
        commands     = [adapt_command_for_platform(c) for c in raw_commands]
        result["FRAMEWORK"] = framework
        result["COMMANDS"]  = commands
        logger.info(f"[MEASURE] framework={framework}  commands={len(commands)}")
        timer.mark("framework_detected")

        # ── 2. Install ─────────────────────────────────────────────────────
        logger.info(f"[MEASURE] {label}: running install commands")
        ok_install, install_err = run_install_commands_compat(raw_commands, repo_path, logger)
        timer.mark("install_done")
        if not ok_install:
            result["ERROR_STAGE"] = "install"
            result["ERROR"]       = install_err
            return result

        # ── 3. Start dev server ────────────────────────────────────────────
        logger.info(f"[MEASURE] {label}: starting dev server on port={port}")
        server_proc = start_server_compat(raw_commands[-1], repo_path, port, logger)
        timer.mark("server_ready")
        if not server_proc:
            result["ERROR_STAGE"] = "server"
            result["ERROR"]       = "dev server failed to start or become healthy"
            return result

        # ── 4. Bore tunnel ─────────────────────────────────────────────────
        active_tunnel: BoreTunnel
        if shared_tunnel is not None:
            logger.info(f"[MEASURE] {label}: reusing shared tunnel")
            active_tunnel = shared_tunnel
        else:
            logger.info(f"[MEASURE] {label}: creating new tunnel for port={port}")
            own_tunnel = BoreTunnel(
                local_port  = port,
                logger      = logger,
                bore_binary = args.bore_binary,
                server      = args.bore_server,
                local_host  = args.bore_local_host,
                secret      = args.bore_secret,
            )
            active_tunnel = own_tunnel

        tunnel_url = active_tunnel.ensure_running()
        timer.mark("tunnel_ready")

        if not tunnel_url:
            result["ERROR_STAGE"] = "tunnel"
            result["ERROR"]       = "Bore tunnel failed to start"
            result["ERROR_TAG"]   = ERR_UNREACHABLE
            return result

        if not is_valid_public_http_url(tunnel_url):
            result["ERROR_STAGE"] = "tunnel"
            result["ERROR"]       = f"Bore returned invalid public URL: {tunnel_url!r}"
            result["ERROR_TAG"]   = ERR_INVALID_TUNNEL
            return result

        result["TUNNEL_URL"] = tunnel_url
        logger.info(f"[MEASURE] {label}: tunnel_url={tunnel_url}")

        # ── 5. Public probe ────────────────────────────────────────────────
        logger.info(f"[MEASURE] {label}: probing public URL")
        probe_ok = probe_public_url(
            tunnel_url,
            timeout_s  = args.public_probe_timeout,
            interval_s = PUBLIC_PROBE_INTERVAL,
            logger     = logger,
        )
        timer.mark("probe_done")
        if not probe_ok:
            result["ERROR_STAGE"] = "tunnel_probe"
            result["ERROR"]       = f"Public tunnel not reachable after {args.public_probe_timeout}s: {tunnel_url}"
            result["ERROR_TAG"]   = ERR_UNREACHABLE
            return result

        # ── 6. PSI call ────────────────────────────────────────────────────
        logger.info(f"[MEASURE] {label}: calling PSI  backend={args.psi_backend}")
        psi_raw, err_tag = rate_limiter.call_psi(
            tunnel_url,
            args.strategy,
            api_key    = args.api_key,
            max_retries= args.max_psi_retries,
            timeout    = args.psi_timeout,
        )
        timer.mark("psi_done")

        if psi_raw is None:
            result["ERROR_STAGE"] = "psi"
            result["ERROR"]       = f"PSI returned no data (error_tag={err_tag})"
            result["ERROR_TAG"]   = err_tag
            return result

        # ── 7. Extract metrics ─────────────────────────────────────────────
        try:
            metrics      = extract_metrics(psi_raw)
            flat_metrics = flatten_metrics(metrics)
        except Exception as exc:
            result["ERROR_STAGE"] = "metrics_parse"
            result["ERROR"]       = repr(exc)
            result["ERROR_TAG"]   = ERR_PARSE_FAILURE
            return result

        result["METRICS"]      = metrics
        result["FLAT_METRICS"] = flat_metrics
        result["STATUS"]       = "ok"
        logger.info(f"[MEASURE] {label}: ✓ OK  perf={flat_metrics.get('score_performance')}")
        return result

    except Exception as exc:
        result["ERROR_STAGE"] = result.get("ERROR_STAGE") or "unexpected"
        result["ERROR"]       = repr(exc)
        result["ERROR_TAG"]   = ERR_UNEXPECTED
        logger.exception(f"[MEASURE] {label}: unexpected exception: {exc}")
        return result

    finally:
        timer.mark("cleanup_done")
        result["TIMING_S"]            = timer.phase_durations()
        result["RATE_LIMIT_SUMMARY"]  = rate_limiter.summary()
        # Always stop server; stop own_tunnel only if we created it
        _cleanup(server_proc, own_tunnel, logger, label=label)


# ---------------------------------------------------------------------------
# Per-repo worker  (runs inside ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _process_repo(
    *,
    row: Dict[str, Any],
    sample_idx: int,
    patch_index: Dict[str, List[Path]],
    args: argparse.Namespace,
    logger,
    outputs: Dict[str, Path],
    checkpoint: CheckpointManager,
    rate_limiter: RateLimitTracker,
    timing_records: List[Dict[str, float]],
    timing_lock: threading.Lock,
    counters: Dict[str, int],
    counters_lock: threading.Lock,
) -> None:
    """
    Full lifecycle for a single repo: clone → baseline → patches → cleanup.

    This function is the unit of parallelism.  Each invocation:
      - owns its own cloned directory (UUID-suffixed → no collisions)
      - owns its own BoreTunnel instance(s)
      - owns its own server process(es)
      - serialises output/checkpoint writes via module-level _OUTPUT_LOCK
      - serialises PSI scheduling via module-level _RATE_LOCK
      - reserves ports via module-level _PORT_LOCK

    It NEVER shares mutable state with other concurrent invocations except
    through the three module-level locks above.
    """
    repo_id    = str(row.get("REPO_ID", "")).strip()
    source_url = str(row.get("URL",     "")).strip()
    sample_id  = str(row.get("ID",      "")).strip()
    branch     = row.get("BRANCH")

    logger.info(f"\n{'='*60}\n[REPO {sample_idx}] {repo_id}\n{'='*60}")

    workspace_dir = (
        Path(args.base_dir).resolve() if args.base_dir else Path.cwd().resolve()
    ) / "tmp_bore_patch_runs"

    repo_path:     Optional[Path]       = None
    shared_tunnel: Optional[BoreTunnel] = None
    shared_port:   Optional[int]        = None

    def _error_result(
        stage: str,
        error: str,
        rtype: str = "baseline",
        pf: Optional[Path] = None,
        pi: Optional[int] = None,
    ) -> Dict[str, Any]:
        return {
            "REPO_ID": repo_id, "SOURCE_URL": source_url, "ID": sample_id,
            "SAMPLE_IDX": sample_idx, "RECORD_TYPE": rtype,
            "RECORD_LABEL": "baseline" if rtype == "baseline" else f"patch:{pf.name if pf else '?'}",
            "PATCH_IDX": pi, "PATCH_FILE": pf.name if pf else None,
            "STRATEGY": args.strategy, "TIMESTAMP": utc_now_iso(),
            "PSI_BACKEND": args.psi_backend,
            "STATUS": "error", "ERROR_STAGE": stage, "ERROR": error,
        }

    try:
        # ── Clone ────────────────────────────────────────────────────────────
        repo_path = clone_repo(repo_id, workspace_dir, logger, branch=branch)
        if not repo_path:
            write_record(outputs, checkpoint, baseline_key(repo_id),
                         _error_result("clone", "repo clone failed"))
            with counters_lock:
                counters["skip"] += 1
            return

        # ── Match patch dir ──────────────────────────────────────────────────
        patch_dir = find_patch_dir_for_repo(repo_id, patch_index)
        if not patch_dir:
            write_record(outputs, checkpoint, baseline_key(repo_id),
                         _error_result("patch_match", f"no patch dir for key={repo_patch_key(repo_id)}"))
            with counters_lock:
                counters["skip"] += 1
            return

        # ── List patch files ─────────────────────────────────────────────────
        patch_files = list_patch_files(patch_dir)
        if not patch_files:
            write_record(outputs, checkpoint, baseline_key(repo_id),
                         _error_result("patches", f"no .patch files in {patch_dir / 'patches'}"))
            with counters_lock:
                counters["skip"] += 1
            return

        logger.info(f"[REPO] patch_dir={patch_dir}  patch_count={len(patch_files)}")

        # ── Shared tunnel/port setup ─────────────────────────────────────────
        if args.reuse_tunnel_per_repo:
            shared_port = find_safe_port(8000 + ((sample_idx * 37) % 1000))
            shared_tunnel = BoreTunnel(
                local_port  = shared_port,
                logger      = logger,
                bore_binary = args.bore_binary,
                server      = args.bore_server,
                local_host  = args.bore_local_host,
                secret      = args.bore_secret,
            )
            logger.info(f"[REPO] Shared port={shared_port} for {repo_id}")

        # ================================================================
        # BASELINE
        # ================================================================
        bkey = baseline_key(repo_id)
        if checkpoint.has(bkey):
            logger.info(f"[BASELINE] Skipping (checkpointed): {repo_id}")
        else:
            logger.info(f"[BASELINE] Resetting repo to HEAD")
            if not reset_repo_to_base(repo_path, logger):
                write_record(outputs, checkpoint, bkey,
                             _error_result("reset", "failed to reset repo for baseline"))
                with counters_lock:
                    counters["skip"] += 1
            else:
                port = (
                    shared_port
                    if shared_port is not None
                    else find_safe_port(8000 + ((sample_idx * 37) % 1000))
                )
                result = deploy_and_measure(
                    repo_path    = repo_path,
                    repo_id      = repo_id,
                    sample_id    = sample_id,
                    source_url   = source_url,
                    sample_idx   = sample_idx,
                    record_type  = "baseline",
                    patch_idx    = None,
                    patch_file   = None,
                    patch_dir    = patch_dir,
                    args         = args,
                    logger       = logger,
                    rate_limiter = rate_limiter,
                    port         = port,
                    shared_tunnel= shared_tunnel if args.reuse_tunnel_per_repo else None,
                )
                if shared_port is None:
                    release_port(port)
                with timing_lock:
                    timing_records.append(result.get("TIMING_S", {}))
                write_record(outputs, checkpoint, bkey, result)
                with counters_lock:
                    if result.get("STATUS") == "ok":
                        counters["success"] += 1
                    else:
                        counters["skip"] += 1

        # ================================================================
        # PATCHES
        # ================================================================
        for j, patch_file in enumerate(patch_files):
            pkey = patch_key(repo_id, patch_file.name)
            if checkpoint.has(pkey):
                logger.info(f"[PATCH {j}] Skipping (checkpointed): {patch_file.name}")
                continue

            logger.info(f"\n[PATCH {sample_idx}.{j}] {patch_file.name}")

            # Reset before every patch
            if not reset_repo_to_base(repo_path, logger):
                write_record(outputs, checkpoint, pkey,
                             _error_result("reset", "failed to reset repo before patch",
                                           rtype="patch", pf=patch_file, pi=j))
                with counters_lock:
                    counters["skip"] += 1
                continue

            # Apply patch
            ok_patch, patch_mode = apply_patch_with_fallback(repo_path, patch_file, logger)
            if not ok_patch:
                logger.error(f"[PATCH {j}] Apply FAILED: {patch_mode}")
                err_res = {
                    **_error_result("patch_apply", patch_mode, rtype="patch", pf=patch_file, pi=j),
                    "PATCH_PATH": str(patch_file), "PATCH_DIR": str(patch_dir),
                    "PATCH_KEY": repo_patch_key(repo_id),
                    "PATCH_APPLY_OK": False, "PATCH_APPLY_MODE": patch_mode,
                }
                write_record(outputs, checkpoint, pkey, err_res)
                with counters_lock:
                    counters["skip"] += 1
                try:
                    reset_repo_to_base(repo_path, logger)
                except Exception:
                    pass
                continue

            logger.info(f"[PATCH {j}] Applied OK  mode={patch_mode}")

            port = (
                shared_port
                if shared_port is not None
                else find_safe_port(8500 + ((sample_idx * 101 + j) % 1000))
            )
            result = deploy_and_measure(
                repo_path    = repo_path,
                repo_id      = repo_id,
                sample_id    = sample_id,
                source_url   = source_url,
                sample_idx   = sample_idx,
                record_type  = "patch",
                patch_idx    = j,
                patch_file   = patch_file,
                patch_dir    = patch_dir,
                args         = args,
                logger       = logger,
                rate_limiter = rate_limiter,
                port         = port,
                shared_tunnel= shared_tunnel if args.reuse_tunnel_per_repo else None,
            )
            if shared_port is None:
                release_port(port)
            result["PATCH_APPLY_OK"]   = True
            result["PATCH_APPLY_MODE"] = patch_mode
            with timing_lock:
                timing_records.append(result.get("TIMING_S", {}))
            write_record(outputs, checkpoint, pkey, result)
            with counters_lock:
                if result.get("STATUS") == "ok":
                    counters["success"] += 1
                else:
                    counters["skip"] += 1

            # Always reset after each patch measurement
            try:
                if not reset_repo_to_base(repo_path, logger):
                    logger.warning(f"[PATCH {j}] Post-measurement reset failed; continuing anyway")
            except Exception as exc:
                logger.warning(f"[PATCH {j}] Post-measurement reset error: {exc}")

    except Exception as exc:
        logger.exception(f"[REPO] Unexpected error processing {repo_id}: {exc}")
        with counters_lock:
            counters["skip"] += 1

    finally:
        # Stop shared tunnel and release shared port
        if shared_tunnel is not None:
            try:
                logger.info(f"[CLEANUP] Stopping shared tunnel for {repo_id}")
                shared_tunnel.stop()
            except Exception as exc:
                logger.warning(f"[CLEANUP] Shared tunnel stop error: {exc}")
        if shared_port is not None:
            release_port(shared_port)

        # Remove cloned repo unless user asked to keep it
        if repo_path is not None and not args.save_cloned_repos:
            try:
                shutil.rmtree(repo_path, ignore_errors=True)
                logger.info(f"[CLEANUP] Removed cloned repo: {repo_path}")
            except Exception as exc:
                logger.warning(f"[CLEANUP] Repo removal error: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Robust baseline + per-patch PSI runner (Bore + checkpointing + parallelism).\n\n"
            "Run with --workers 1 (default) for serial behaviour identical to v2.\n"
            "Run with --workers N>1 to process N repos concurrently.\n"
            "Each worker owns its own cloned directory, server process, and Bore tunnel;\n"
            "shared state (outputs, checkpoint, PSI rate-limiter) is protected by locks."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input-csv",    required=True, help="CSV with REPO_ID column")
    p.add_argument("--patches-root", required=True, help="Root folder of per-repo patch directories")
    p.add_argument("--limit",  type=int, default=30,  help="Max repos to process")
    p.add_argument("--offset", type=int, default=0,   help="Starting offset in matched-repo list")
    p.add_argument("--strategy", choices=["mobile", "desktop"], default="mobile")
    p.add_argument(
        "--api-key",
        default=(os.getenv("PSI_API_KEY") or os.getenv("GOOGLE_PAGESPEED_INSIGHTS_API_KEY")),
        help="Google PSI API key",
    )
    p.add_argument("--psi-backend", choices=["google", "adobe"], default="google")
    p.add_argument(
        "--delay",
        type=float, default=10.0,
        help=(
            "Minimum inter-PSI-call delay in seconds (global, shared across all workers). "
            "Google enforces a floor of 8 s regardless. "
            "With --workers N the effective per-repo rate is delay*N seconds between "
            "consecutive PSI calls for any one repo."
        ),
    )
    p.add_argument("--max-psi-retries", type=int,   default=5)
    p.add_argument("--psi-timeout",     type=int,   default=120)
    p.add_argument("--public-probe-timeout", type=int, default=40)
    p.add_argument("--out",            default="patched_bore_psi_results.jsonl")
    p.add_argument("--checkpoint-file",default=None)
    p.add_argument("--summary-csv",    default=None)
    p.add_argument("--report-md",      default=None)
    p.add_argument("--base-dir",       default=None)
    p.add_argument("--save-cloned-repos", action="store_true")
    p.add_argument("--fresh",          action="store_true", help="Delete old outputs/checkpoint before running")

    p.add_argument("--bore-binary",     default="bore")
    p.add_argument("--bore-server",     default="bore.pub")
    p.add_argument("--bore-local-host", default="localhost")
    p.add_argument("--bore-secret",     default=None)
    p.add_argument("--reuse-tunnel-per-repo",    action="store_true",  default=True,
                   help="Reuse a single Bore tunnel for all measurements of the same repo (default: on).")
    p.add_argument("--no-reuse-tunnel-per-repo", dest="reuse_tunnel_per_repo", action="store_false",
                   help="Create a fresh Bore tunnel for every baseline/patch measurement.")

    p.add_argument(
        "--workers",
        type=int, default=1, metavar="N",
        help=(
            "Number of repos to process concurrently (default: 1 = serial, identical to v2). "
            "Each worker clones its own repo copy, runs its own server and Bore tunnel. "
            "Shared resources (outputs, checkpoint, PSI rate-limiter) are protected by locks. "
            "Recommended: start with 2–4 and raise --delay proportionally to avoid PSI 429s. "
            "Example: --workers 4 --delay 40"
        ),
    )

    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.workers < 1:
        raise ValueError("--workers must be >= 1")

    base_dir      = Path(args.base_dir).resolve() if args.base_dir else Path.cwd().resolve()
    workspace_dir = base_dir / "tmp_bore_patch_runs"
    log_dir       = base_dir / "logs" / "bulk_patch_psi_bore"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    input_csv    = Path(args.input_csv).expanduser().resolve()
    patches_root = Path(args.patches_root).expanduser().resolve()
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")
    if not patches_root.exists():
        raise FileNotFoundError(f"Patches root not found: {patches_root}")

    out_path         = (Path.cwd() / args.out).resolve()
    checkpoint_path  = (Path(args.checkpoint_file).expanduser().resolve()
                        if args.checkpoint_file
                        else out_path.with_suffix(out_path.suffix + ".checkpoint.json"))
    summary_csv_path = (Path(args.summary_csv).expanduser().resolve()
                        if args.summary_csv
                        else out_path.with_suffix(".summary.csv"))
    report_md_path   = (Path(args.report_md).expanduser().resolve()
                        if args.report_md
                        else out_path.with_suffix(".report.md"))

    if args.fresh:
        for p in [out_path, checkpoint_path, summary_csv_path, report_md_path]:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = get_logger("bulk_patch_psi_bore_fixed", log_dir / f"run_{ts}.log")
    logger.info(
        f"Run starting — workers={args.workers} limit={args.limit} offset={args.offset} "
        f"strategy={args.strategy} delay={args.delay}s backend={args.psi_backend} "
        f"bore_server={args.bore_server} reuse_tunnel_per_repo={args.reuse_tunnel_per_repo}"
    )
    for label, val in [
        ("Input CSV",       input_csv),
        ("Patches root",    patches_root),
        ("Workspace",       workspace_dir),
        ("JSONL",           out_path),
        ("Summary CSV",     summary_csv_path),
        ("Markdown report", report_md_path),
        ("Checkpoint",      checkpoint_path),
    ]:
        logger.info(f"  {label:<18}: {val}")

    checkpoint = CheckpointManager(
        checkpoint_path,
        {
            "input_csv": str(input_csv), "patches_root": str(patches_root),
            "limit": args.limit, "offset": args.offset, "strategy": args.strategy,
            "psi_backend": args.psi_backend, "delay": args.delay,
            "max_psi_retries": args.max_psi_retries, "psi_timeout": args.psi_timeout,
            "bore_server": args.bore_server, "reuse_tunnel_per_repo": args.reuse_tunnel_per_repo,
            "workers": args.workers,
        },
    )
    outputs = {"jsonl": out_path, "csv": summary_csv_path, "md": report_md_path}

    all_rows    = load_targets_from_csv(input_csv, logger)
    patch_index = build_patch_index(patches_root)
    logger.info(f"Indexed {sum(len(v) for v in patch_index.values())} patch directories")

    matched_rows:    List[Dict[str, Any]] = []
    unmatched_repos: List[str]            = []
    for row in all_rows:
        rid = str(row.get("REPO_ID", "")).strip()
        if find_patch_dir_for_repo(rid, patch_index):
            matched_rows.append(row)
        else:
            unmatched_repos.append(rid)

    logger.info(f"Matched {len(matched_rows)} / {len(all_rows)} rows to patch dirs")
    if unmatched_repos:
        logger.info(f"First unmatched: {unmatched_repos[:10]}")

    end_idx = min(args.offset + args.limit, len(matched_rows))
    rows    = matched_rows[args.offset:end_idx]
    logger.info(f"Selected {len(rows)} repos (rows {args.offset}–{end_idx - 1 if rows else args.offset})")

    rate_limiter = RateLimitTracker(min_delay=args.delay, backend=args.psi_backend, logger=logger)

    # Shared mutable state for workers (protected by their respective locks)
    timing_records: List[Dict[str, float]] = []
    timing_lock    = threading.Lock()
    counters       = {"success": 0, "skip": 0}
    counters_lock  = threading.Lock()

    # tqdm progress bar – update it from any worker thread safely
    pbar = tqdm(total=len(rows), desc="BORE_PATCH_PSI")

    def _worker(row: Dict[str, Any], sample_idx: int) -> None:
        try:
            _process_repo(
                row           = row,
                sample_idx    = sample_idx,
                patch_index   = patch_index,
                args          = args,
                logger        = logger,
                outputs       = outputs,
                checkpoint    = checkpoint,
                rate_limiter  = rate_limiter,
                timing_records= timing_records,
                timing_lock   = timing_lock,
                counters      = counters,
                counters_lock = counters_lock,
            )
        finally:
            pbar.update(1)

    if args.workers == 1:
        # Serial path – identical behaviour to original script
        for i, row in enumerate(rows):
            _worker(row, args.offset + i)
    else:
        # Parallel path
        logger.info(f"Starting ThreadPoolExecutor with {args.workers} workers")
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(_worker, row, args.offset + i): (args.offset + i, row)
                for i, row in enumerate(rows)
            }
            for future in as_completed(futures):
                idx, row = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    repo_id = str(row.get("REPO_ID", "?"))
                    logger.exception(f"[MAIN] Unhandled exception from worker for {repo_id} (idx={idx}): {exc}")

    pbar.close()

    with counters_lock:
        success_count = counters["success"]
        skip_count    = counters["skip"]

    rl = rate_limiter.summary()
    logger.info(f"\nRun complete: {success_count} succeeded, {skip_count} errored/skipped")
    logger.info(f"Rate-limit summary: {rl}")
    for label, p in [
        ("JSONL",       out_path),
        ("CSV",         summary_csv_path),
        ("MD",          report_md_path),
        ("Checkpoint",  checkpoint_path),
    ]:
        logger.info(f"  {label:<12}: {p}")
    with timing_lock:
        print_timing_summary(timing_records)


if __name__ == "__main__":
    main()
