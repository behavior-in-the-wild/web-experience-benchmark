"""
Clone N git repos in parallel and expose each via ngrok or Cloudflare tunnel.

Usage:
    # from input.csv  (REPO_ID + FRAMEWORK columns)
    python tunnel.py --input input.csv --provider ngrok
    python tunnel.py --input input.csv --provider cloudflare

    # from a JSON file
    python tunnel.py --repos repos.json --provider ngrok

repos.json schema:
  [
    {
      "url":       "https://github.com/user/repo",   # required
      "dir":       "local-folder-name",              # optional, derived from url if omitted
      "serve_cmd": "npm run dev"                     # optional, runs instead of static server
    },
    ...
  ]

Environment variables:
  NGROK_AUTHTOKEN  – required when --provider ngrok
"""

import argparse
import asyncio
import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────────────────────────────────────
# Framework → serve command mapping
# All repos here are pre-built static GitHub Pages sites, so we default to
# Python's built-in static server.  Override per-framework if you need to.
# ──────────────────────────────────────────────────────────────────────────────
FRAMEWORK_SERVE_CMD: dict[str, Optional[str]] = {
    "Express": None,   # static files served directly
    "Hexo":    None,   # pre-built public/ already in repo root
    "Jekyll":  None,
    "Hugo":    None,
    "Next.js": None,
    "Gatsby":  None,
}


def load_csv(path: str) -> list[dict]:
    """
    Read input.csv and convert each row to the repos-list format.

    Expected columns (case-insensitive):
        REPO_ID   – e.g. "Batophobia/Batophobia.github.io"
        FRAMEWORK – e.g. "Express", "Hexo"
    """
    repos: list[dict] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        # normalise header names to upper-case for robust matching
        for raw_row in reader:
            row = {k.strip().upper(): v for k, v in raw_row.items()}
            repo_id   = row.get("REPO_ID", "").strip()
            framework = row.get("FRAMEWORK", "").strip()
            if not repo_id:
                continue
            url       = f"https://github.com/{repo_id}"
            dir_name  = repo_id.replace("/", "__")   # "User__repo"
            serve_cmd = FRAMEWORK_SERVE_CMD.get(framework, None)
            repos.append({"url": url, "dir": dir_name, "serve_cmd": serve_cmd,
                          "framework": framework})
    return repos

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def log(tag: str, msg: str, color: str = RESET) -> None:
    print(f"{color}{BOLD}[{tag}]{RESET} {msg}", flush=True)


def repo_dir_from_url(url: str) -> str:
    """Derive a local directory name from a git URL."""
    name = url.rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 – Clone repos in parallel
# ──────────────────────────────────────────────────────────────────────────────

def clone_repo(entry: dict, clone_root: Path) -> tuple[bool, dict]:
    """Clone a single repo; return (success, enriched_entry)."""
    url    = entry["url"]
    folder = entry.get("dir") or repo_dir_from_url(url)
    dest   = clone_root / folder

    if dest.exists():
        log(folder, f"directory already exists – skipping clone", YELLOW)
    else:
        log(folder, f"cloning {url} …", CYAN)
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log(folder, f"clone FAILED:\n{result.stderr.strip()}", RED)
            return False, entry
        log(folder, "clone OK", GREEN)

    return True, {**entry, "dir": folder, "path": str(dest)}


def clone_all_parallel(repos: list[dict], clone_root: Path) -> list[dict]:
    """Clone every repo concurrently; return only the successful ones."""
    successful: list[dict] = []

    with ThreadPoolExecutor(max_workers=len(repos) or 1) as pool:
        futures = {pool.submit(clone_repo, r, clone_root): r for r in repos}
        for fut in as_completed(futures):
            ok, enriched = fut.result()
            if ok:
                successful.append(enriched)

    return successful


# ──────────────────────────────────────────────────────────────────────────────
# Step 2 – Serve each repo locally
# ──────────────────────────────────────────────────────────────────────────────

_server_threads: list[threading.Thread] = []
_subprocesses:   list[subprocess.Popen] = []


def _make_silent_handler(directory: str):
    """SimpleHTTPRequestHandler that serves from a fixed directory silently."""
    class SilentHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, fmt, *args):  # suppress per-request logs
            pass

    return SilentHandler


def start_static_server(path: str, port: int) -> None:
    """Serve a directory with Python's built-in HTTP server (background thread)."""
    handler = _make_silent_handler(path)
    server  = HTTPServer(("127.0.0.1", port), handler)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _server_threads.append(t)
    log(Path(path).name, f"static server on http://127.0.0.1:{port}", GREEN)


def start_custom_server(serve_cmd: str, path: str, port: int) -> None:
    """Run an arbitrary serve command (e.g. npm run dev) as a subprocess."""
    env = {**os.environ, "PORT": str(port)}
    proc = subprocess.Popen(
        serve_cmd,
        shell=True,
        cwd=path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _subprocesses.append(proc)
    log(Path(path).name, f"custom server '{serve_cmd}' on port {port}", GREEN)


def assign_ports(repos: list[dict], base_port: int = 8000) -> list[dict]:
    """Attach a unique local port to every repo entry."""
    return [{**r, "port": base_port + i} for i, r in enumerate(repos)]


def start_all_servers(repos: list[dict]) -> None:
    for repo in repos:
        path      = repo["path"]
        port      = repo["port"]
        serve_cmd = repo.get("serve_cmd")

        if serve_cmd:
            start_custom_server(serve_cmd, path, port)
        else:
            start_static_server(path, port)

    # Give custom servers a moment to bind
    time.sleep(1.5)


# ──────────────────────────────────────────────────────────────────────────────
# Step 3 – Create tunnels
# ──────────────────────────────────────────────────────────────────────────────

NGROK_FREE_TUNNEL_LIMIT = 3


def open_ngrok_tunnels(repos: list[dict]) -> list[dict]:
    """
    Writes a temporary ngrok config file and runs `ngrok start --all`.
    This is the official multi-tunnel approach — all tunnels open in parallel
    in a single ngrok agent session.

    Free plan  : up to 3 tunnels  ← enforced here
    Pro        : up to 10 tunnels
    """
    import json as _json
    import urllib.request

    token = os.environ.get("NGROK_AUTHTOKEN", "")
    if not token:
        log("ngrok", "NGROK_AUTHTOKEN not set — set it and retry", RED)
        sys.exit(1)

    if len(repos) > NGROK_FREE_TUNNEL_LIMIT:
        log(
            "ngrok",
            f"free plan allows {NGROK_FREE_TUNNEL_LIMIT} tunnels max — "
            f"got {len(repos)}. Capping to first {NGROK_FREE_TUNNEL_LIMIT}. "
            f"Use --provider cloudflare for unlimited free tunnels.",
            YELLOW,
        )
        repos = repos[:NGROK_FREE_TUNNEL_LIMIT]

    # ── build ngrok v3 config ─────────────────────────────────────────────────
    cfg: dict = {
        "version": "3",
        "agent":   {"authtoken": token},
        "tunnels": {},
    }
    for repo in repos:
        # ngrok tunnel names must be alphanumeric + hyphens
        name = repo["dir"].replace("_", "-").replace("/", "-")[:40]
        cfg["tunnels"][name] = {"proto": "http", "addr": repo["port"]}

    cfg_path = Path("ngrok_generated.yml")
    import yaml as _yaml   # PyYAML; falls back to manual serialisation if absent
    try:
        cfg_path.write_text(_yaml.dump(cfg, default_flow_style=False), encoding="utf-8")
    except ImportError:
        # Manually write a minimal YAML without PyYAML
        lines = [
            f'version: "3"',
            f'agent:',
            f'  authtoken: {token}',
            f'tunnels:',
        ]
        for name, tun in cfg["tunnels"].items():
            lines += [f'  {name}:', f'    proto: http', f'    addr: {tun["addr"]}']
        cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    log("ngrok", f"config written → {cfg_path}", CYAN)
    log("ngrok", f"starting {len(repos)} tunnel(s) with `ngrok start --all` …", CYAN)

    proc = subprocess.Popen(
        ["ngrok", "start", "--all", "--config", str(cfg_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _subprocesses.append(proc)

    # ── poll the ngrok local API until all tunnels are up ────────────────────
    api_url  = "http://127.0.0.1:4040/api/tunnels"
    deadline = time.time() + 30
    tunnels_by_port: dict[int, str] = {}

    while time.time() < deadline:
        time.sleep(1)
        try:
            with urllib.request.urlopen(api_url, timeout=2) as resp:
                data = _json.loads(resp.read())
            for t in data.get("tunnels", []):
                if t.get("proto") != "https":
                    continue
                addr = t.get("config", {}).get("addr", "")
                # addr may be "localhost:8000" or just "8000"
                port = int(addr.split(":")[-1])
                # only store if we haven't seen this port yet (avoid http duplicate)
                if port not in tunnels_by_port:
                    tunnels_by_port[port] = t["public_url"]
            if len(tunnels_by_port) >= len(repos):
                break
        except Exception:
            pass  # ngrok not ready yet

    enriched = []
    for repo in repos:
        url = tunnels_by_port.get(repo["port"])
        if url:
            log(repo["dir"], f"ngrok tunnel → {url}", CYAN)
        else:
            log(repo["dir"], "tunnel URL not found (check ngrok plan limits)", RED)
        enriched.append({**repo, "public_url": url})

    return enriched


def _start_cloudflare_proc(repo: dict) -> tuple[dict, subprocess.Popen]:
    """Launch a cloudflared process and return immediately (non-blocking)."""
    port = repo["port"]
    log(repo["dir"], f"starting cloudflared on port {port} …", CYAN)
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    _subprocesses.append(proc)
    return repo, proc


def _collect_cloudflare_url(repo: dict, proc: subprocess.Popen) -> dict:
    """Block until the trycloudflare.com URL appears in cloudflared's stderr."""
    public_url = _wait_for_cloudflare_url(proc)
    if public_url:
        log(repo["dir"], f"cloudflare tunnel → {public_url}", CYAN)
    else:
        log(repo["dir"], "could not read tunnel URL from cloudflared", RED)
    return {**repo, "public_url": public_url}


def open_cloudflare_tunnels(repos: list[dict]) -> list[dict]:
    """
    Launch all cloudflared processes simultaneously, then collect their URLs
    in parallel — so all tunnels come up at once instead of one by one.
    """
    # ── start every process at the same time ─────────────────────────────────
    launched: list[tuple[dict, subprocess.Popen]] = []
    for repo in repos:
        launched.append(_start_cloudflare_proc(repo))

    # ── collect URLs in parallel ──────────────────────────────────────────────
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=len(launched)) as pool:
        futures = {
            pool.submit(_collect_cloudflare_url, repo, proc): i
            for i, (repo, proc) in enumerate(launched)
        }
        for fut in as_completed(futures):
            idx          = futures[fut]
            results[idx] = fut.result()

    return [results[i] for i in range(len(launched))]


def _wait_for_cloudflare_url(proc: subprocess.Popen, timeout: int = 60) -> Optional[str]:
    """Read cloudflared stderr until a *.trycloudflare.com URL appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            break
        for token in line.split():
            token = token.strip().rstrip("|").rstrip(".")
            if token.startswith("https://") and "trycloudflare.com" in token:
                return token
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Bore tunnels  (https://github.com/ekzhang/bore)
# bore local <port> --to bore.pub
# Public URL format: http://bore.pub:<assigned_port>
# No account, no rate limits documented, unlimited tunnels.
# ──────────────────────────────────────────────────────────────────────────────

def _wait_for_bore_url(proc: subprocess.Popen, timeout: int = 30) -> Optional[str]:
    """Read bore's stdout until the assigned bore.pub:<port> line appears.

    bore writes its tracing log to stdout (not stderr).  Uses a background
    reader thread so the timeout is always respected even when bore produces
    no output (readline() would block until EOF otherwise).
    """
    import queue
    import re

    result_q: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:
                # bore logs: "... listening at bore.pub:12345"
                match = re.search(r"bore\.pub:(\d+)", line)
                if match:
                    result_q.put(f"http://bore.pub:{match.group(1)}")
                    return
        except Exception:
            pass
        result_q.put(None)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        return result_q.get(timeout=timeout)
    except queue.Empty:
        return None


def _start_bore_proc(repo: dict) -> tuple[dict, subprocess.Popen]:
    port = repo["port"]
    log(repo["dir"], f"starting bore on port {port} …", CYAN)
    proc = subprocess.Popen(
        ["bore", "local", str(port), "--to", "bore.pub"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "RUST_LOG": "info"},
    )
    _subprocesses.append(proc)
    return repo, proc


def _collect_bore_url(repo: dict, proc: subprocess.Popen) -> dict:
    public_url = _wait_for_bore_url(proc)
    if public_url:
        log(repo["dir"], f"bore tunnel → {public_url}", CYAN)
    else:
        log(repo["dir"], "could not read tunnel URL from bore", RED)
    return {**repo, "public_url": public_url}


def open_bore_tunnels(repos: list[dict]) -> list[dict]:
    """Launch all bore processes simultaneously and collect URLs in parallel."""
    launched = [_start_bore_proc(repo) for repo in repos]

    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=len(launched)) as pool:
        futures = {
            pool.submit(_collect_bore_url, repo, proc): i
            for i, (repo, proc) in enumerate(launched)
        }
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    return [results[i] for i in range(len(launched))]


# ──────────────────────────────────────────────────────────────────────────────
# Summary printer
# ──────────────────────────────────────────────────────────────────────────────

def print_summary(repos: list[dict]) -> None:
    print()
    print(f"{BOLD}{'─' * 70}{RESET}")
    print(f"{BOLD}  LIVE TUNNELS  ({len(repos)} repos){RESET}")
    print(f"{BOLD}{'─' * 70}{RESET}")
    for r in repos:
        url       = r.get("public_url") or "N/A"
        local     = f"http://127.0.0.1:{r['port']}"
        framework = r.get("framework", "")
        label     = r['dir'][:28]
        fw_tag    = f" [{framework}]" if framework else ""
        print(f"  {GREEN}{label:<28}{RESET}{YELLOW}{fw_tag:<12}{RESET}  {url}")
        print(f"  {'':28}{'':12}  local → {local}")
        print()
    print(f"{BOLD}{'─' * 70}{RESET}")
    print("Press Ctrl+C to stop all tunnels and servers.\n")


# ──────────────────────────────────────────────────────────────────────────────
# Stress test – find Cloudflare concurrent request limit
# ──────────────────────────────────────────────────────────────────────────────

STRESS_LEVELS = [10, 25, 50, 75, 100, 150, 200, 250, 300]


async def _fetch(session, url: str) -> int:
    """Single GET; returns HTTP status code or 0 on error."""
    try:
        async with session.get(url, timeout=15) as resp:
            await resp.read()
            return resp.status
    except Exception:
        return 0


async def _stress_one(url: str, label: str) -> dict:
    """
    Ramp up concurrent requests against `url`.
    At each level fire all N requests simultaneously and record status counts.
    Stops as soon as 429s appear (rate-limit found) or all levels pass.
    """
    try:
        import aiohttp
    except ImportError:
        print(f"{RED}aiohttp not installed. Run: pip install aiohttp{RESET}")
        return {"label": label, "url": url, "limit_found_at": None, "rounds": []}

    rounds = []
    limit_found_at: Optional[int] = None

    print(f"\n{BOLD}[stress:{label[:20]}]{RESET} starting ramp …")

    async with aiohttp.ClientSession() as session:
        for n in STRESS_LEVELS:
            tasks   = [_fetch(session, url) for _ in range(n)]
            t0      = time.perf_counter()
            results = await asyncio.gather(*tasks)
            elapsed = time.perf_counter() - t0
            counts  = dict(Counter(results))

            ok      = counts.get(200, 0)
            err429  = counts.get(429, 0)
            errors  = counts.get(0,   0)

            color = RED if err429 else (YELLOW if errors else GREEN)
            log(
                f"stress:{label[:16]}",
                f"concurrent={n:>3}  200={ok:>3}  429={err429:>3}  "
                f"err={errors:>3}  time={elapsed:.2f}s",
                color,
            )
            rounds.append({"n": n, "counts": counts, "elapsed_s": round(elapsed, 2)})

            if err429:
                limit_found_at = n
                log(f"stress:{label[:16]}", f"429s first appeared at n={n}", YELLOW)
                break

            await asyncio.sleep(0.8)   # let cloudflare recover between rounds

    return {"label": label, "url": url, "limit_found_at": limit_found_at, "rounds": rounds}


async def _run_all_stress(repos: list[dict]) -> None:
    """Run stress tests against all live tunnel URLs in parallel."""
    targets = [r for r in repos if r.get("public_url")]
    if not targets:
        log("stress", "no live tunnel URLs to test", RED)
        return

    log("stress", f"testing {len(targets)} tunnel(s) in parallel …", CYAN)
    results = await asyncio.gather(*[
        _stress_one(r["public_url"], r["dir"]) for r in targets
    ])

    # ── print summary table ───────────────────────────────────────────────────
    print(f"\n{BOLD}{'─' * 70}{RESET}")
    print(f"{BOLD}  STRESS TEST RESULTS{RESET}")
    print(f"{BOLD}{'─' * 70}{RESET}")
    for res in results:
        found = res["limit_found_at"]
        if found:
            verdict = f"{RED}429s at n={found}{RESET}"
        else:
            verdict = f"{GREEN}no 429s up to n={STRESS_LEVELS[-1]}{RESET}"
        print(f"  {res['label'][:40]:<40}  {verdict}")
        for r in res["rounds"]:
            bar = "█" * (r["n"] // 10)
            print(f"    n={r['n']:>3}  {bar:<30}  {r['counts']}")
        print()
    print(f"{BOLD}{'─' * 70}{RESET}\n")


def run_stress_tests(repos: list[dict]) -> None:
    asyncio.run(_run_all_stress(repos))


# ──────────────────────────────────────────────────────────────────────────────
# Cycle test – find Cloudflare tunnel creation rate limit
# ──────────────────────────────────────────────────────────────────────────────

def _kill_proc(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def _spawn_tunnel_proc(provider: str, port: int) -> tuple[subprocess.Popen, callable]:
    """Start a tunnel process for the given provider; return (proc, url_reader)."""
    if provider == "bore":
        proc = subprocess.Popen(
            ["bore", "local", str(port), "--to", "bore.pub"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env={**os.environ, "RUST_LOG": "info"},
        )
        return proc, lambda p: _wait_for_bore_url(p, timeout=30)
    else:  # cloudflare
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        return proc, lambda p: _wait_for_cloudflare_url(p, timeout=30)


def _independent_cycle_loop(
    port:       int,
    label:      str,
    provider:   str,
    max_cycles: int,
    delay_s:    float,
    print_lock: threading.Lock,
    stop_event: threading.Event,
    stats:      dict,
) -> None:
    """
    One tunnel's independent loop: create → get URL → kill → repeat.
    Runs in its own thread, completely independent of the other tunnels.
    """
    ok    = 0
    fail  = 0
    consec_fail = 0
    start = time.perf_counter()

    for cycle in range(1, max_cycles + 1):
        if stop_event.is_set():
            break

        t0              = time.perf_counter()
        proc, read_url  = _spawn_tunnel_proc(provider, port)
        url             = read_url(proc)
        elapsed         = time.perf_counter() - t0

        _kill_proc(proc)

        total = time.perf_counter() - start

        if url:
            ok         += 1
            consec_fail = 0
            color       = GREEN
            result      = f"OK    {url[:38]}"
        else:
            fail        += 1
            consec_fail += 1
            color        = RED
            result       = "FAILED"

        with print_lock:
            print(
                f"  {color}{label:<22}{RESET}  "
                f"cycle={cycle:>4}  {color}{result:<44}{RESET}  "
                f"{elapsed:>6.2f}s  (total {total:>6.1f}s)"
            )

        if consec_fail >= 3:
            with print_lock:
                log(label, "3 consecutive failures — stopping this tunnel's loop", RED)
            break

        if delay_s > 0:
            time.sleep(delay_s)

    stats[label] = {
        "ok":    ok,
        "fail":  fail,
        "total": time.perf_counter() - start,
    }


def run_cycle_test(
    parallel:   int   = 4,
    base_port:  int   = 7990,
    max_cycles: int   = 200,
    delay_s:    float = 0.0,
    provider:   str   = "cloudflare",
) -> None:
    """
    Runs `parallel` independent cycle loops simultaneously — each tunnel
    creates, gets its URL, kills itself, and immediately starts the next
    cycle without waiting for the others.
    """
    ports  = list(range(base_port, base_port + parallel))
    labels = [f"tunnel[{i}]:{p}" for i, p in enumerate(ports)]

    log("cycle-test",
        f"provider={provider}  parallel={parallel}  ports={ports[0]}–{ports[-1]}  "
        f"max_cycles={max_cycles}  delay={delay_s}s", CYAN)
    print(f"  {'tunnel':<22}  {'cycle/result':<52}  {'time':>6}")
    print(f"  {'─'*22}  {'─'*52}  {'─'*6}")

    print_lock = threading.Lock()
    stop_event = threading.Event()
    stats: dict = {}

    threads = [
        threading.Thread(
            target=_independent_cycle_loop,
            args=(ports[i], labels[i], provider, max_cycles, delay_s,
                  print_lock, stop_event, stats),
            daemon=True,
        )
        for i in range(parallel)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # ── summary ───────────────────────────────────────────────────────────────
    print()
    print(f"{BOLD}{'─' * 70}{RESET}")
    print(f"{BOLD}  CYCLE TEST SUMMARY{RESET}")
    print(f"{BOLD}{'─' * 70}{RESET}")
    total_ok = total_fail = 0
    for label in labels:
        s     = stats.get(label, {})
        ok    = s.get("ok",    0)
        fail  = s.get("fail",  0)
        secs  = s.get("total", 0)
        rate  = ok / secs * 60 if secs > 0 else 0
        total_ok   += ok
        total_fail += fail
        print(f"  {label:<24}  ok={GREEN}{ok:>4}{RESET}  "
              f"fail={RED}{fail:>3}{RESET}  "
              f"rate={rate:>5.1f} creations/min")
    print()
    print(f"  All tunnels combined  :  "
          f"ok={GREEN}{total_ok}{RESET}  fail={RED}{total_fail}{RESET}  "
          f"total={total_ok+total_fail} creations")
    print(f"{BOLD}{'─' * 70}{RESET}\n")


# ──────────────────────────────────────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────────────────────────────────────

def _shutdown(signum=None, frame=None) -> None:
    print(f"\n{YELLOW}Shutting down …{RESET}")
    for proc in _subprocesses:
        try:
            proc.terminate()
        except Exception:
            pass
    sys.exit(0)


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Clone N repos in parallel and expose each via a public tunnel."
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--input", metavar="CSV",
        help="Path to input CSV file with REPO_ID and FRAMEWORK columns",
    )
    src.add_argument(
        "--repos", default="repos.json",
        help="Path to repos JSON file (default: repos.json)",
    )
    p.add_argument(
        "--provider", choices=["ngrok", "cloudflare", "bore"], default="cloudflare",
        help="Tunnel provider: cloudflare (default), bore, ngrok",
    )
    p.add_argument(
        "--base-port", type=int, default=8000,
        help="First local port to use (default: 8000)",
    )
    p.add_argument(
        "--clone-dir", default="cloned_repos",
        help="Root directory for cloned repos (default: cloned_repos/)",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Only process first N repos from the file (0 = all)",
    )
    p.add_argument(
        "--stress-test", action="store_true",
        help="After tunnels are up, ramp concurrent requests to find the rate limit",
    )
    p.add_argument(
        "--cycle-test", action="store_true",
        help="Repeatedly create+kill tunnels to find the provider's rate limit",
    )
    p.add_argument(
        "--cycle-provider", choices=["cloudflare", "bore"], default="cloudflare",
        help="Provider to use for cycle test (default: cloudflare)",
    )
    p.add_argument(
        "--cycle-parallel", type=int, default=4,
        help="Number of tunnels to open in parallel per cycle (default: 4)",
    )
    p.add_argument(
        "--cycle-delay", type=float, default=0.0,
        help="Seconds to wait between cycle-test batches (default: 0 = as fast as possible)",
    )
    p.add_argument(
        "--cycle-max", type=int, default=200,
        help="Maximum number of create→kill cycles to attempt (default: 200)",
    )
    return p.parse_args()


def main() -> None:
    args       = parse_args()

    # ── Cycle test is standalone – no cloning or serving needed ──────────────
    if args.cycle_test:
        run_cycle_test(
            parallel   = args.cycle_parallel,
            max_cycles = args.cycle_max,
            delay_s    = args.cycle_delay,
            provider   = args.cycle_provider,
        )
        return

    clone_root = Path(args.clone_dir)
    clone_root.mkdir(exist_ok=True)

    # ── Load repo list ────────────────────────────────────────────────────────
    if args.input:
        repos: list[dict] = load_csv(args.input)
        log("config", f"loaded {len(repos)} repos from {args.input}", CYAN)
    else:
        with open(args.repos, encoding="utf-8") as fh:
            repos = json.load(fh)
        log("config", f"loaded {len(repos)} repos from {args.repos}", CYAN)

    if args.limit and args.limit > 0:
        repos = repos[: args.limit]
        log("config", f"limited to first {len(repos)} repos (--limit {args.limit})", YELLOW)

    if not repos:
        print("No repos defined in the config. Exiting.")
        sys.exit(0)

    print(f"\n{BOLD}Cloning {len(repos)} repo(s) …{RESET}\n")

    # ── Clone in parallel ─────────────────────────────────────────────────────
    cloned = clone_all_parallel(repos, clone_root)
    if not cloned:
        print(f"{RED}No repos were cloned successfully. Exiting.{RESET}")
        sys.exit(1)

    print(f"\n{BOLD}Starting local servers …{RESET}\n")

    # ── Assign ports and start servers ────────────────────────────────────────
    cloned = assign_ports(cloned, base_port=args.base_port)
    start_all_servers(cloned)

    if args.provider == "ngrok":
        log("ngrok", f"free plan = {NGROK_FREE_TUNNEL_LIMIT} tunnels max. "
            "Use --provider cloudflare or --provider bore for unlimited tunnels.", YELLOW)

    print(f"\n{BOLD}Opening {args.provider} tunnels …{RESET}\n")

    # ── Open tunnels ──────────────────────────────────────────────────────────
    if args.provider == "ngrok":
        live = open_ngrok_tunnels(cloned)
    elif args.provider == "bore":
        live = open_bore_tunnels(cloned)
    else:
        live = open_cloudflare_tunnels(cloned)

    # ── Print summary ─────────────────────────────────────────────────────────
    print_summary(live)

    # ── Stress test (optional) ────────────────────────────────────────────────
    if args.stress_test:
        log("stress", "tunnels are up — starting stress test in 3 s …", YELLOW)
        time.sleep(3)
        run_stress_tests(live)
        log("stress", "done. tunnels still running. Ctrl+C to stop.", GREEN)

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()
