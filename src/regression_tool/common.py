"""
Shared infrastructure for regression evaluation on code patches.

Handles:
- CSV template map loading (ID → repo info)
- Git repo cloning at a pinned commit
- Starting/stopping framework servers via harness host_files scripts
- Taking full-page screenshots via Playwright
- Fetching rendered HTML via Playwright
- Capturing browser console errors via Playwright
- Applying git patches
"""

from __future__ import annotations

import csv
import json
import logging
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from docker_tool.hosting import HostResult, start_host, stop_host

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parents[2]       # repo root
_HARNESS_DIR = _ROOT_DIR / "harness"
_CSV_PATH = _HARNESS_DIR / "SAMPLE" / "input.csv"
_CODE_PATCHES_DIR = _ROOT_DIR / "code_patches"

# Map framework name (from CSV) → host_files script stem
_FRAMEWORK_SCRIPT = {
    "Express":     "host_express",
    "Static HTML": "host_static_html",
    "Jekyll":      "host_jekyll",
    "Hugo":        "host_hugo",
    "Hexo":        "host_hexo",
    "Pelican":     "host_pelican",
    "Quarto":      "host_quarto",
    "React":       "host_react",
    "Vue":         "host_vue",
    "Next.js":     "host_next",
}


# ---------------------------------------------------------------------------
# CSV template map
# ---------------------------------------------------------------------------

def load_template_map(csv_path: Path = _CSV_PATH) -> dict[int, dict]:
    """Return a dict mapping template ID (int) → repo info dict."""
    tmap: dict[int, dict] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                tid = int(row["ID"])
            except (KeyError, ValueError):
                continue
            tmap[tid] = {
                "repo_id":        row.get("REPO_ID", ""),
                "commit_id":      row.get("COMMIT_ID", "").strip(),
                "framework":      row.get("FRAMEWORK", "Static HTML"),
                "host_file_path": row.get("HOST_FILE_PATH", ""),
            }
    return tmap


import threading

_PORT_LOCK = threading.Lock()
_ALLOCATED_PORTS = set()

def find_free_port(start: int = 14000, end: int = 15000) -> int:
    """
    Find a free TCP port in [start, end).

    Uses the process PID to pick a starting offset so that parallel workers
    (each with a different PID) search different parts of the range first,
    drastically reducing port-collision probability.
    """
    import os as _os
    pid_offset = (_os.getpid() % 200) * 4   # spread 200 workers × 4 ports apart
    search_start = start + pid_offset
    # Search from pid-offset first, then wrap around to the beginning
    with _PORT_LOCK:
        for port in list(range(search_start, end)) + list(range(start, search_start)):
            if port in _ALLOCATED_PORTS:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    _ALLOCATED_PORTS.add(port)
                    return port
                except OSError:
                    continue
    raise RuntimeError(f"No free port found in [{start}, {end})")

def release_port(port: int) -> None:
    """Release a port back into the pool."""
    with _PORT_LOCK:
        _ALLOCATED_PORTS.discard(port)


def wait_for_server(port: int, timeout: int = 45) -> bool:
    """Poll localhost:port until it accepts connections or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(1)
    return False


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def clone_repo(repo_id: str, commit_id: str, dest_dir: Path) -> bool:
    """
    Clone *repo_id* from GitHub into *dest_dir* and check out *commit_id*.
    Returns True on success.
    """
    import shutil
    import tempfile
    
    cache_base = Path.home() / ".cache" / "web_benchmark_repos"
    cache_base.mkdir(parents=True, exist_ok=True)
    
    safe_repo_id = repo_id.replace("/", "_")
    safe_commit = commit_id if commit_id and commit_id not in ("", " ", "null") else "HEAD"
    cache_dir = cache_base / f"{safe_repo_id}_{safe_commit}"

    if cache_dir.exists() and (cache_dir / ".git").exists():
        logger.info("Using cached repo for %s at %s", repo_id, safe_commit)
        shutil.copytree(cache_dir, dest_dir, symlinks=True, dirs_exist_ok=True)
        return True

    tmp_dir = Path(tempfile.mkdtemp(dir=str(cache_base), prefix="tmp_clone_"))
    
    url = f"https://github.com/{repo_id}.git"
    logger.info("Cloning %s ...", url)
    try:
        r = subprocess.run(
            ["git", "clone", "--depth=1", url, str(tmp_dir)],
            capture_output=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.error("git clone timed out for %s", url)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False
    if r.returncode != 0:
        logger.error("git clone failed: %s", r.stderr.decode())
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False

    if commit_id and commit_id not in ("", " ", "null"):
        r2 = subprocess.run(
            ["git", "-C", str(tmp_dir), "checkout", commit_id],
            capture_output=True, timeout=30,
        )
        if r2.returncode != 0:
            logger.warning("git checkout %s failed, using HEAD: %s",
                           commit_id, r2.stderr.decode())

    # Commit baseline so `git apply` has a clean index
    subprocess.run(["git", "-C", str(tmp_dir), "add", "-A"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(tmp_dir), "commit", "-qm", "baseline"],
                   capture_output=True)

    try:
        tmp_dir.rename(cache_dir)
    except OSError:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Copy to destination
    shutil.copytree(cache_dir, dest_dir, symlinks=True, dirs_exist_ok=True)
    return True


def apply_patch(repo_dir: Path, patch_file: Path) -> bool:
    """Apply a git patch to *repo_dir*. Returns True on success."""
    if not patch_file.exists() or patch_file.stat().st_size == 0:
        logger.warning("Patch file missing or empty: %s", patch_file)
        return False

    r = subprocess.run(
        ["git", "-C", str(repo_dir), "apply", "--whitespace=nowarn",
         str(patch_file)],
        capture_output=True,
    )
    if r.returncode == 0:
        return True

    if r.returncode != 0:
        # --3way handles "Directory not empty" and context conflicts
        logger.warning("git apply failed, trying --3way: %s", r.stderr.decode())
        r2 = subprocess.run(
            ["git", "-C", str(repo_dir), "apply", "--3way",
             "--whitespace=nowarn", str(patch_file)],
            capture_output=True,
        )
        if r2.returncode == 0:
            return True

        if r2.returncode != 0:
            logger.warning("git apply --3way failed, trying --reject: %s",
                           r2.stderr.decode())
            r3 = subprocess.run(
                ["git", "-C", str(repo_dir), "apply", "--reject",
                 "--whitespace=nowarn", str(patch_file)],
                capture_output=True,
            )
            if r3.returncode == 0:
                logger.warning(
                    "Patch applied with rejects for %s; partial apply likely.",
                    patch_file,
                )
                return False
            return False
    return False


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def start_server(repo_dir: Path, framework: str, port: int) -> subprocess.Popen | HostResult:
    """
    Start the framework server for *repo_dir* on *port*.
    Returns a HostResult for sandboxed hosting or a Popen handle for legacy hosting.
    """
    if os.getenv("HOST_SANDBOX", "1").strip().lower() not in {"0", "false", "no"}:
        result = start_host(
            repo_dir=repo_dir,
            framework=framework,
            port=port,
            log=repo_dir.parent / "host.log",
            mode=os.getenv("SANDBOX_MODE", "auto"),
        )
        if result.status != "success":
            raise RuntimeError(result.error or "server startup failed")
        return result

    script_stem = _FRAMEWORK_SCRIPT.get(framework, "host_static_html")
    host_script = _HARNESS_DIR / "host_files" / f"{script_stem}.sh"

    if not host_script.exists():
        logger.warning("Host script not found: %s — falling back to static", host_script)
        host_script = _HARNESS_DIR / "host_files" / "host_static_html.sh"

    env = {**os.environ, "PORT": str(port)}
    proc = subprocess.Popen(
        ["bash", str(host_script), str(repo_dir), "/dev/null"],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def stop_server(proc: subprocess.Popen | HostResult) -> None:
    """Terminate the server process group."""
    if proc is None:
        return
    if isinstance(proc, HostResult):
        stop_host(container_id=proc.container_id, pid=proc.pid)
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


# ---------------------------------------------------------------------------
# Playwright helpers (sync)
# ---------------------------------------------------------------------------

def take_screenshot(url: str, output_path: Path, width: int = 1280) -> bool:
    """Take a viewport screenshot of *url* via direct CDP.

    Uses CDP `Page.captureScreenshot` instead of Playwright's `page.screenshot`
    because the latter wraps the capture in a `document.fonts.ready` wait + an
    implicit timeout that fires before CDP produces a stable frame on heavy
    pages (e.g. editmysite/wsite templates with many @font-face declarations
    and continuous tracking JS). Direct CDP just grabs whatever pixels exist;
    we use a 30-second outer subprocess kill as the hard ceiling.
    """
    import base64
    from playwright.sync_api import sync_playwright

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": width, "height": 800})
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=15_000)
            except Exception:
                pass  # screenshot whatever state the page is in
            client = ctx.new_cdp_session(page)
            # captureBeyondViewport=False: viewport-only capture; default `true`
            # fails on heavy pages with non-trivial scroll height ("Unable to
            # capture screenshot" protocol error). Matches `full_page=False`
            # semantics from page.screenshot.
            # Retry: first attempt sometimes fails before compositor is ready
            # ("Unable to capture screenshot"); a short wait fixes it.
            last_err: Exception | None = None
            data: str | None = None
            for _ in range(4):
                try:
                    result = client.send(
                        "Page.captureScreenshot",
                        {"format": "png", "captureBeyondViewport": False},
                    )
                    data = result["data"]
                    break
                except Exception as cdp_exc:
                    last_err = cdp_exc
                    page.wait_for_timeout(2000)
            if data is None:
                raise RuntimeError(f"CDP screenshot failed after retries: {last_err}")
            output_path.write_bytes(base64.b64decode(data))
            browser.close()
        return True
    except Exception as exc:
        logger.error("Screenshot failed for %s: %s", url, exc)
        return False


def fetch_html(url: str) -> str:
    """
    Fetch fully-rendered HTML with all CSS, fonts, and images inlined as
    data URIs so the saved file is self-contained (no live server needed).

    Strategy: attach a response listener before navigation so every asset the
    browser loads is captured in-flight, then post-process the DOM to replace
    external references with inline equivalents.
    """
    import base64
    import re
    import urllib.parse
    from playwright.sync_api import sync_playwright

    # url → (body_bytes, mime_type)
    captured: dict[str, tuple[bytes, str]] = {}

    def _on_response(resp) -> None:
        ct = resp.headers.get("content-type", "")
        mime = ct.split(";")[0].strip()
        if resp.status == 200 and any(t in mime for t in (
            "text/css", "image/", "font/", "application/font",
        )):
            try:
                captured[resp.url] = (resp.body(), mime)
            except Exception:
                pass

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.on("response", _on_response)
            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)

            # Force lazy-loaded images to load by:
            # 1. Setting all lazy images to eager
            # 2. Scrolling through the page to trigger any remaining lazy loads
            try:
                page.evaluate("""() => {
                    document.querySelectorAll('img[loading="lazy"]').forEach(img => {
                        img.loading = 'eager';
                        img.src = img.src;  // re-trigger load
                    });
                }""")
                page.evaluate("""async () => {
                    await new Promise(resolve => {
                        let pos = 0;
                        const step = () => {
                            pos += window.innerHeight;
                            window.scrollTo(0, pos);
                            if (pos < document.body.scrollHeight) {
                                setTimeout(step, 100);
                            } else {
                                window.scrollTo(0, 0);
                                setTimeout(resolve, 500);
                            }
                        };
                        step();
                    });
                }""")
                # Wait for any newly triggered requests to settle
                page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass

            html = page.content()

            # Fetch any root-relative assets still missing from captured dict
            # (e.g. images that were truly never requested by the browser)
            # Note: pattern excludes protocol-relative (//cdn...) and absolute URLs
            missing_srcs = set(re.findall(r'src=["\'](/[^/"\'> ][^"\'> ]*)["\']', html))
            missing_hrefs = set(
                g for m in re.findall(
                    r'<link\b[^>]*\brel=["\']stylesheet["\'][^>]*\bhref=["\'](/[^"\'> ]+)["\']'
                    r'|<link\b[^>]*\bhref=["\'](/[^"\'> ]+)["\'][^>]*\brel=["\']stylesheet["\']',
                    html, re.I
                ) for g in (m if isinstance(m, tuple) else (m,)) if g
            )
            for path in missing_srcs | missing_hrefs:
                abs_url = urllib.parse.urljoin(url, path)
                if abs_url not in captured:
                    try:
                        resp = page.request.get(abs_url, timeout=5_000)
                        if resp.ok:
                            ct = resp.headers.get("content-type", "application/octet-stream")
                            mime = ct.split(";")[0].strip()
                            captured[abs_url] = (resp.body(), mime)
                    except Exception:
                        pass

            browser.close()
    except Exception as exc:
        logger.error("HTML fetch failed for %s: %s", url, exc)
        return ""

    def _resolve(href: str, base: str = url) -> str:
        if href.startswith(("data:", "blob:", "//")):
            return href
        return urllib.parse.urljoin(base, href)

    def _data_uri(res_url: str) -> str | None:
        entry = captured.get(res_url)
        if entry is None:
            # also try origin-relative form
            entry = captured.get(_resolve(res_url))
        if entry is None:
            return None
        body, mime = entry
        return f"data:{mime};base64,{base64.b64encode(body).decode()}"

    def _inline_css_urls(css: str, css_base_url: str) -> str:
        """Replace url(...) references inside a CSS string with data URIs."""
        def _repl(m: re.Match) -> str:
            raw = m.group(1).strip("'\" ")
            if raw.startswith("data:"):
                return m.group(0)
            uri = _data_uri(_resolve(raw, css_base_url))
            return f"url('{uri}')" if uri else m.group(0)
        return re.sub(r"url\(\s*([^)]+?)\s*\)", _repl, css)

    def _replace_link_tag(m: re.Match) -> str:
        """Inline a <link rel="stylesheet"> as a <style> block."""
        href = next((g for g in m.groups() if g), None)
        if not href:
            return m.group(0)
        res_url = _resolve(href)
        entry = captured.get(res_url)
        if entry is None:
            return m.group(0)
        css_text = entry[0].decode("utf-8", errors="replace")
        css_text = _inline_css_urls(css_text, res_url)
        return f"<style>{css_text}</style>"

    def _replace_img_src(m: re.Match) -> str:
        """Replace an <img src="..."> with a data URI."""
        tag, src = m.group(0), m.group(1)
        if src.startswith("data:"):
            return tag
        uri = _data_uri(_resolve(src))
        return tag.replace(src, uri, 1) if uri else tag

    # 1. Inline <link rel="stylesheet" href="..."> (both attribute orderings)
    html = re.sub(
        r'<link\b[^>]*\brel=["\']stylesheet["\'][^>]*\bhref=["\']([^"\']+)["\'][^>]*/?>|'
        r'<link\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*\brel=["\']stylesheet["\'][^>]*/?>',
        _replace_link_tag, html, flags=re.IGNORECASE,
    )

    # 2. Inline <img src="...">
    html = re.sub(
        r'<img\b[^>]+\bsrc=["\']([^"\']+)["\']',
        _replace_img_src, html, flags=re.IGNORECASE,
    )

    # 3. Inline <source srcset="url"> (single-URL form used in <picture>)
    def _replace_srcset(m: re.Match) -> str:
        tag, srcset = m.group(0), m.group(1)
        parts = []
        for entry in srcset.split(","):
            entry = entry.strip()
            bits = entry.split()
            if bits:
                uri = _data_uri(_resolve(bits[0]))
                if uri:
                    bits[0] = uri
            parts.append(" ".join(bits))
        return tag.replace(srcset, ", ".join(parts), 1)

    html = re.sub(
        r'<source\b[^>]+\bsrcset=["\']([^"\']+)["\']',
        _replace_srcset, html, flags=re.IGNORECASE,
    )

    return html


def capture_console_errors(url: str) -> list[str]:
    """
    Load *url* in a headless browser and return all console error messages
    and uncaught JS exceptions observed during page load.
    """
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()

            page.on("console", lambda msg: errors.append(msg.text)
                    if msg.type == "error" else None)
            page.on("pageerror", lambda err: errors.append(str(err)))

            try:
                page.goto(url, wait_until="networkidle", timeout=30_000)
            except Exception:
                pass

            browser.close()
    except Exception as exc:
        logger.error("Console capture failed for %s: %s", url, exc)

    return errors


# ---------------------------------------------------------------------------
# Metadata loader
# ---------------------------------------------------------------------------

def load_cwv_metadata(patch_file: Path) -> dict[str, Any]:
    """
    Load CWV aggregated metrics from the *_desktop.json and *_mobile.json
    files that live alongside *patch_file* in the same agent folder.

    Returns a dict with keys 'desktop' and 'mobile', each containing the
    'aggregated' sub-dict (or {} if the file is absent / malformed).
    """
    stem = patch_file.stem  # e.g. "101_template_claudecode"

    def _read(suffix: str) -> dict:
        p = patch_file.parent / f"{stem}_{suffix}.json"
        if not p.exists():
            return {}
        try:
            text = p.read_text(encoding="utf-8")
            # Files may have log lines prepended before the JSON object
            brace = text.find("{")
            if brace > 0:
                text = text[brace:]
            data = json.loads(text)
            return data.get("aggregated", data)
        except Exception:
            return {}

    return {
        "desktop": _read("desktop"),
        "mobile":  _read("mobile"),
    }


# ---------------------------------------------------------------------------
# High-level "snapshot" helper used by both tools
# ---------------------------------------------------------------------------

def snapshot_site(
    repo_dir: Path,
    framework: str,
    port: int,
    screenshot_path: Path,
    html_path: Path,
) -> dict[str, Any]:
    """
    Start the server, wait for it to be ready, then capture:
        - A full-page screenshot  → *screenshot_path*
        - The rendered HTML       → *html_path*
        - Browser console errors  → list[str]

    Returns a dict with keys: ok (bool), console_errors (list[str]).
    Stops the server before returning.
    """
    proc = start_server(repo_dir, framework, port)
    url = f"http://localhost:{port}/"

    if not wait_for_server(port, timeout=90):
        stop_server(proc)
        logger.error("Server never became ready on port %d", port)
        return {"ok": False, "console_errors": []}

    ss_ok = take_screenshot(url, screenshot_path)
    html  = fetch_html(url)
    errs  = capture_console_errors(url)

    stop_server(proc)
    time.sleep(1)   # let the port fully release

    if html and html_path:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")

    return {"ok": ss_ok, "console_errors": errs}
