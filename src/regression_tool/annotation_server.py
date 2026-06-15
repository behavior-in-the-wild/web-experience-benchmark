#!/usr/bin/env python3
"""
annotation_server.py — Side-by-side manual annotation UI.

Annotators open http://localhost:7000, log in with their name, and label
each agent patch as IMPROVED / SAME / REGRESSION / UNSURE by comparing
baseline vs patched screenshots (and optional HTML snapshots).

Usage:
    pip install flask
    python3 scripts/evaluation/annotation_server.py

Options:
    --port PORT        Web server port (default: 7000)
    --results RESULTS  Path to results/ dir (default: scripts/evaluation/regression_eval/results)
    --out OUT          Annotations CSV output path (default: <results>/manual_annotations.csv)
"""

import argparse
import atexit
import csv
import os
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import (
        Flask, redirect, render_template_string, request,
        send_file, session, url_for, Response,
    )
except ImportError:
    sys.exit("Install Flask first:  pip install flask")

from common import (
    apply_patch,
    clone_repo,
    find_free_port,
    load_template_map,
    start_server,
    stop_server,
    wait_for_server,
)

app = Flask(__name__)
app.secret_key = "bench-annotate-2024"

# Runtime globals set in main()
G: dict = {}

LABELS = [
    "SAME",
    "MISSING_CONTENT",
    "BROKEN_LAYOUT",
    "STYLING_ISSUE",
    "FUNCTIONAL_ERROR",
    "EXTRA_CONTENT",
    "OTHER",
    "UNSURE",
]

LABEL_CFG = {
    "SAME":             {"bg": "#3b82f6", "border": "#2563eb", "key": "1", "icon": "➡️", "tw": "blue"},
    "MISSING_CONTENT":  {"bg": "#ef4444", "border": "#dc2626", "key": "2", "icon": "🗑️", "tw": "red"},
    "BROKEN_LAYOUT":    {"bg": "#f97316", "border": "#ea580c", "key": "3", "icon": "🧱", "tw": "orange"},
    "STYLING_ISSUE":    {"bg": "#eab308", "border": "#ca8a04", "key": "4", "icon": "🎨", "tw": "yellow"},
    "FUNCTIONAL_ERROR": {"bg": "#db2777", "border": "#be185d", "key": "5", "icon": "⚙️", "tw": "pink"},
    "EXTRA_CONTENT":    {"bg": "#8b5cf6", "border": "#7c3aed", "key": "6", "icon": "➕", "tw": "purple"},
    "OTHER":            {"bg": "#64748b", "border": "#475569", "key": "7", "icon": "📝", "tw": "slate"},
    "UNSURE":           {"bg": "#a8a29e", "border": "#78716c", "key": "8", "icon": "❓", "tw": "stone"},
}

# ─────────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────────

_EVAL_FIELDS = (
    "v1_gpt", "v1_dom_lsh", "v1_jaccard", "v1_console", "v1_overall",
    "v2_structural", "v2_gpt", "v2_jaccard", "v2_console", "v2_overall",
    "error",
)

def _load_template_map(csv_path: Path) -> dict:
    """Return {template_id_str: framework_str} from the harness input CSV."""
    tmap: dict = {}
    if not csv_path.exists():
        return tmap
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tid = row.get("ID", "").strip()
            if tid:
                tmap[tid] = row.get("FRAMEWORK", "")
    return tmap


def _load_tasks(patches_dir: Path, results_dir: Path, template_map: dict) -> list:
    """
    Discover tasks strictly from results_dir/eval_results.csv.
    The CSV is the source of truth (trimmed/filtered rows).
    """
    blank = {f: "" for f in _EVAL_FIELDS}
    tasks: dict[tuple, dict] = {}
    csv_path = results_dir / "eval_results.csv"
    if not csv_path.exists():
        return []

    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            agent = row.get("agent", "").strip()
            tid = row.get("template_id", "").strip()
            if not agent or not tid:
                continue
            key = (agent, tid)
            if key not in tasks:
                tasks[key] = {
                    "agent": agent,
                    "template_id": tid,
                    "framework": row.get("framework", "") or template_map.get(tid, ""),
                    **blank,
                }
            tasks[key].update({f: row.get(f, "") for f in _EVAL_FIELDS})
            if not tasks[key]["framework"] and row.get("framework"):
                tasks[key]["framework"] = row["framework"]

    return list(tasks.values())


def _load_annotations(ann_file: Path) -> dict:
    data: dict = {}
    if not ann_file.exists():
        return data
    with open(ann_file) as f:
        for row in csv.DictReader(f):
            key = (row.get("agent", ""), row.get("template_id", ""))
            data.setdefault(key, []).append(row)
    return data


def _append_annotation(ann_file: Path, row: dict, lock: threading.Lock) -> None:
    with lock:
        write_header = not ann_file.exists()
        with open(ann_file, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if write_header:
                w.writeheader()
            w.writerow(row)



def _find_html(results_dir: Path, agent: str, tid: str, kind: str) -> Path | None:
    for ver in ("v2", "v1"):
        p = results_dir / agent / f"{tid}_template_{agent}" / ver / f"{kind}.html"
        if p.exists():
            return p
    return None


def _find_patch_file(patches_dir: Path, agent: str, tid: str) -> Path | None:
    pdir = patches_dir / f"results_{agent}"
    if not pdir.exists():
        return None
    matches = sorted(pdir.glob(f"{tid}_template_{agent}.patch"))
    return matches[0] if matches else None


def _setup_base(tid: str, tinfo: dict):
    base_tmp = tempfile.TemporaryDirectory(prefix="ann_live_base_", dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
    base_repo = Path(base_tmp.name) / "repo"
    if not clone_repo(tinfo["repo_id"], tinfo["commit_id"], base_repo):
        base_tmp.cleanup()
        return None
    base_port = find_free_port()
    base_proc = start_server(base_repo, tinfo["framework"], base_port)
    if not wait_for_server(base_port, timeout=90):
        stop_server(base_proc)
        base_tmp.cleanup()
        return None
    return {
        "url": f"http://127.0.0.1:{base_port}/",
        "proc": base_proc,
        "tmp": base_tmp,
    }

def _setup_patch(task: dict, tinfo: dict, patch_file: Path):
    pat_tmp = tempfile.TemporaryDirectory(prefix="ann_live_patch_", dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
    pat_repo = Path(pat_tmp.name) / "repo"
    if not clone_repo(tinfo["repo_id"], tinfo["commit_id"], pat_repo):
        pat_tmp.cleanup()
        return None
    if not apply_patch(pat_repo, patch_file):
        pat_tmp.cleanup()
        return None
    pat_port = find_free_port()
    pat_proc = start_server(pat_repo, tinfo["framework"], pat_port)
    if not wait_for_server(pat_port, timeout=90):
        stop_server(pat_proc)
        pat_tmp.cleanup()
        return None
    return {
        "url": f"http://127.0.0.1:{pat_port}/",
        "proc": pat_proc,
        "tmp": pat_tmp,
    }

def _ensure_live_session(task: dict) -> dict | None:
    key = (task["agent"], task["template_id"])
    now = time.time()
    with G["lock"]:
        if "baseline_servers" not in G:
            G["baseline_servers"] = {}
        existing = G["live_sessions"].get(key)
        if existing:
            existing["last_used"] = now
            return existing

    tinfo = G["template_info_map"].get(task["template_id"])
    if not tinfo:
        return None
    patch_file = _find_patch_file(G["patches_dir"], task["agent"], task["template_id"])
    if not patch_file:
        return None

    tid = task["template_id"]
    
    import concurrent.futures
    
    with G["lock"]:
        if "baseline_servers_ready" not in G:
            G["baseline_servers_ready"] = {}
            G["baseline_locks"] = {}
            
        if tid not in G["baseline_locks"]:
            import threading
            G["baseline_locks"][tid] = threading.Lock()

    # Function to ensure base is setup synchronously so we don't deadlock
    def get_or_setup_base():
        with G["baseline_locks"][tid]:
            if tid in G["baseline_servers_ready"]:
                return G["baseline_servers_ready"][tid]
            res = _setup_base(tid, tinfo)
            if res:
                G["baseline_servers_ready"][tid] = res
            return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_base = ex.submit(get_or_setup_base)
        fut_patch = ex.submit(_setup_patch, task, tinfo, patch_file)
        res_base = fut_base.result()
        res_patch = fut_patch.result()
    if res_base is None or res_patch is None:
        if res_patch is not None:
            stop_server(res_patch["proc"])
            res_patch["tmp"].cleanup()
            from common import release_port
            pat_port = int(res_patch["url"].split(":")[-1].strip("/"))
            release_port(pat_port)
        return None

    session = {
        "baseline_url": res_base["url"],
        "patched_url": res_patch["url"],
        "baseline_proc": res_base["proc"], # For backwards compat with _close_live_session, but we shouldn't kill it
        "patched_proc": res_patch["proc"],
        "baseline_tmp": res_base["tmp"],
        "patched_tmp": res_patch["tmp"],
        "last_used": now,
        "tid": tid,
    }
    with G["lock"]:
        G["live_sessions"][key] = session
    return session

def _close_live_session(session: dict) -> None:
    # Only kill patched proc, keep baseline alive for reuse
    stop_server(session.get("patched_proc"))
    pt = session.get("patched_tmp")
    if pt:
        pt.cleanup()
        
    pat_url = session.get("patched_url")
    if pat_url:
        from common import release_port
        pat_port = int(pat_url.split(":")[-1].strip("/"))
        release_port(pat_port)


def _evict_live_sessions(force_trim: bool = False) -> None:
    now = time.time()
    ttl = G["live_ttl_sec"]
    max_sessions = G["max_live_sessions"]

    to_close: list[dict] = []
    with G["lock"]:
        # TTL eviction first
        for key, sess in list(G["live_sessions"].items()):
            if now - sess.get("last_used", now) > ttl:
                to_close.append(sess)
                del G["live_sessions"][key]

        # LRU trim if needed
        if force_trim or len(G["live_sessions"]) > max_sessions:
            ordered = sorted(
                G["live_sessions"].items(),
                key=lambda kv: kv[1].get("last_used", 0)
            )
            while len(ordered) > max_sessions:
                key, sess = ordered.pop(0)
                to_close.append(sess)
                G["live_sessions"].pop(key, None)

    for sess in to_close:
        _close_live_session(sess)


def _warm_task(task: dict) -> None:
    key = (task["agent"], task["template_id"])
    try:
        # Skip if already ready
        with G["lock"]:
            if key in G["live_sessions"]:
                G["live_sessions"][key]["last_used"] = time.time()
                return
            fail_until = G["live_fail_until"].get(key, 0)
            if time.time() < fail_until:
                return
        sess = _ensure_live_session(task)
        if sess is None:
            with G["lock"]:
                n = G["live_fail_count"].get(key, 0) + 1
                G["live_fail_count"][key] = n
                backoff = min(1800, 60 * (2 ** min(n, 4)))
                G["live_fail_until"][key] = time.time() + backoff
        _evict_live_sessions(force_trim=True)
        _fill_live_pool()
    finally:
        with G["lock"]:
            G["live_inflight"].discard(key)


def _schedule_prewarm(tasks: list, current_idx: int) -> None:
    n = len(tasks)
    if n == 0:
        return
    count = max(0, G["prewarm_count"])
    for off in range(1, count + 1):
        t = tasks[(current_idx + off) % n]
        key = (t["agent"], t["template_id"])
        should_submit = False
        with G["lock"]:
            if key not in G["live_sessions"] and key not in G["live_inflight"]:
                G["live_inflight"].add(key)
                should_submit = True
        if should_submit:
            G["prewarm_pool"].submit(_warm_task, t)


def _fill_live_pool() -> None:
    tasks = G["tasks"]
    if not tasks:
        return
    to_submit = []
    with G["lock"]:
        target = max(0, G["max_live_sessions"])
        active = len(G["live_sessions"]) + len(G["live_inflight"])
        needed = max(0, target - active)
        if needed == 0:
            return

        n = len(tasks)
        tries = 0
        while needed > 0 and tries < n * 2:
            t = tasks[G["queue_idx"] % n]
            G["queue_idx"] = (G["queue_idx"] + 1) % n
            tries += 1
            key = (t["agent"], t["template_id"])
            if key in G["live_sessions"] or key in G["live_inflight"]:
                continue
            if time.time() < G["live_fail_until"].get(key, 0):
                continue
            G["live_inflight"].add(key)
            to_submit.append(t)
            needed -= 1

    for t in to_submit:
        G["prewarm_pool"].submit(_warm_task, t)


def _kill_live_session_for(agent: str, tid: str) -> bool:
    key = (agent, tid)
    sess = None
    with G["lock"]:
        sess = G["live_sessions"].pop(key, None)
        G["live_inflight"].discard(key)
    if sess:
        _close_live_session(sess)
        _fill_live_pool()
        return True
    _fill_live_pool()
    return False


def _cleanup_live_sessions() -> None:
    with G["lock"]:
        sessions = list(G.get("live_sessions", {}).items())
        G["live_sessions"] = {}
    for _, s in sessions:
        _close_live_session(s)


def _next_unannotated(tasks: list, anns: dict, annotator: str, current_idx: int) -> dict | None:
    n = len(tasks)
    for offset in range(1, n + 1):
        t = tasks[(current_idx + offset) % n]
        key = (t["agent"], t["template_id"])
        if not any(a["annotator"] == annotator for a in anns.get(key, [])):
            return t
    return None


def _annotator_done_count(tasks: list, anns: dict, annotator: str) -> int:
    return sum(
        1 for t in tasks
        if any(a["annotator"] == annotator
               for a in anns.get((t["agent"], t["template_id"]), []))
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not session.get("annotator"):
        return redirect(url_for("login"))
    return redirect(url_for("task_list"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if len(name) >= 2:
            session["annotator"] = name
            return redirect(url_for("task_list"))
        error = "Name must be at least 2 characters."
    return render_template_string(LOGIN_T, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/tasks")
def task_list():
    if not session.get("annotator"):
        return redirect(url_for("login"))
    annotator = session["annotator"]
    tasks     = G["tasks"]
    anns      = G["annotations"]

    af = request.args.get("agent", "")
    ff = request.args.get("framework", "")
    sf = request.args.get("status", "pending")

    rows = []
    for i, t in enumerate(tasks):
        key   = (t["agent"], t["template_id"])
        my    = [a for a in anns.get(key, []) if a["annotator"] == annotator]
        all_a = anns.get(key, [])
        rows.append({
            **t,
            "idx":      i,
            "my_label": my[-1]["label"] if my else "",
            "all_count": len(all_a),
            "has_html": _find_html(G["results_dir"], t["agent"], t["template_id"], "baseline") is not None,
        })

    if af:
        rows = [r for r in rows if r["agent"] == af]
    if ff:
        rows = [r for r in rows if r["framework"] == ff]
    if sf == "pending":
        rows = [r for r in rows if not r["my_label"]]
    elif sf == "done":
        rows = [r for r in rows if r["my_label"]]

    total = len(tasks)
    done  = _annotator_done_count(tasks, anns, annotator)
    agents     = sorted(set(t["agent"]     for t in tasks))
    frameworks = sorted(set(t["framework"] for t in tasks))

    return render_template_string(
        TASKS_T,
        rows=rows, annotator=annotator,
        agents=agents, frameworks=frameworks,
        af=af, ff=ff, sf=sf,
        total=total, done=done,
        label_cfg=LABEL_CFG,
    )


@app.route("/annotate/<agent>/<template_id>")
def annotate(agent, template_id):
    if not session.get("annotator"):
        return redirect(url_for("login"))
    annotator = session["annotator"]
    tasks     = G["tasks"]
    anns      = G["annotations"]

    task = next((t for t in tasks
                 if t["agent"] == agent and t["template_id"] == template_id), None)
    if not task:
        return "Task not found", 404

    idx    = next(i for i, t in enumerate(tasks)
                  if t["agent"] == agent and t["template_id"] == template_id)
    _evict_live_sessions()
    prev_t = tasks[idx - 1] if idx > 0 else None
    next_t = tasks[idx + 1] if idx < len(tasks) - 1 else None
    next_u = _next_unannotated(tasks, anns, annotator, idx)

    key    = (agent, template_id)
    my_ann = [a for a in anns.get(key, []) if a["annotator"] == annotator]
    existing = my_ann[-1] if my_ann else None
    others   = [a for a in anns.get(key, []) if a["annotator"] != annotator]

    total = len(tasks)
    done  = _annotator_done_count(tasks, anns, annotator)

    has_bl_html = _find_html(G["results_dir"], agent, template_id, "baseline") is not None
    has_pt_html = _find_html(G["results_dir"], agent, template_id, "patched")  is not None
    live = _ensure_live_session(task)
    baseline_src = f"/live/{agent}/{template_id}/baseline/" if live else f"/snapshot/{agent}/{template_id}/baseline"
    patched_src = f"/live/{agent}/{template_id}/patched/" if live else f"/snapshot/{agent}/{template_id}/patched"
    _schedule_prewarm(tasks, idx)
    _fill_live_pool()

    return render_template_string(
        ANNOTATE_T,
        task=task, annotator=annotator,
        has_bl_html=has_bl_html, has_pt_html=has_pt_html,
        live_enabled=bool(live), baseline_src=baseline_src, patched_src=patched_src,
        existing=existing, others=others,
        prev_t=prev_t, next_t=next_t, next_u=next_u,
        task_idx=idx + 1, total=total, done=done,
        labels=LABELS, label_cfg=LABEL_CFG,
    )


@app.route("/submit", methods=["POST"])
def submit():
    if not session.get("annotator"):
        return redirect(url_for("login"))
    annotator = session["annotator"]
    agent     = request.form.get("agent", "")
    tid       = request.form.get("template_id", "")
    label     = request.form.get("label", "")
    notes     = request.form.get("notes", "").strip()
    elapsed   = request.form.get("elapsed", "0")

    if label not in LABELS:
        return "Invalid label", 400

    task = next((t for t in G["tasks"]
                 if t["agent"] == agent and t["template_id"] == tid), None)
    if not task:
        return "Task not found", 404

    row = dict(
        timestamp=datetime.now(timezone.utc).isoformat(),
        annotator=annotator, agent=agent, template_id=tid,
        framework=task["framework"], label=label,
        notes=notes, time_seconds=elapsed,
    )
    _append_annotation(G["ann_file"], row, G["lock"])
    with G["lock"]:
        G["annotations"].setdefault((agent, tid), []).append(row)
    # Free the current live session after annotation and backfill next queued task.
    _kill_live_session_for(agent, tid)

    na = request.form.get("next_agent", "")
    nt = request.form.get("next_tid", "")
    if na and nt:
        return redirect(url_for("annotate", agent=na, template_id=nt))
    return redirect(url_for("task_list"))


@app.route("/live/kill", methods=["POST"])
def kill_live():
    if not session.get("annotator"):
        return redirect(url_for("login"))
    agent = request.form.get("agent", "").strip()
    tid = request.form.get("template_id", "").strip()
    next_agent = request.form.get("next_agent", "").strip()
    next_tid = request.form.get("next_tid", "").strip()
    if agent and tid:
        _kill_live_session_for(agent, tid)
    if next_agent and next_tid:
        return redirect(url_for("annotate", agent=next_agent, template_id=next_tid))
    return redirect(url_for("task_list"))



INJECT_SCRIPT = b"""<script>
(function(){
  try {
    var p = window.parent;
    if(!p || p === window) return;
    var kind = window.location.pathname.includes('/baseline') ? 'baseline' : 'patched';
    var origErr = console.error;
    console.error = function() {
        var args = Array.prototype.slice.call(arguments);
        origErr.apply(console, args);
        var msg = args.map(function(a){ return typeof a === 'object' ? JSON.stringify(a) : String(a); }).join(' ');
        p.postMessage({type: 'console_error', kind: kind, msg: msg}, '*');
    };
    window.addEventListener('error', function(e) {
        p.postMessage({type: 'console_error', kind: kind, msg: e.message + ' at ' + e.filename + ':' + e.lineno}, '*');
    }, true);
    window.addEventListener('unhandledrejection', function(e) {
        p.postMessage({type: 'console_error', kind: kind, msg: 'Unhandled Promise Rejection: ' + e.reason}, '*');
    }, true);
  } catch(e) {}
})();
</script>"""

def _inject_script(body: bytes) -> bytes:
    idx = body.find(b"<head>")
    if idx != -1:
        return body[:idx + 6] + INJECT_SCRIPT + body[idx + 6:]
    idx = body.find(b"<html")
    if idx != -1:
        end = body.find(b">", idx)
        if end != -1:
            return body[:end + 1] + INJECT_SCRIPT + body[end + 1:]
    return INJECT_SCRIPT + body

@app.route("/snapshot/<agent>/<tid>/<kind>")
def serve_snapshot(agent, tid, kind):
    if kind not in ("baseline", "patched"):
        return "Not found", 404
    path = _find_html(G["results_dir"], agent, tid, kind)
    if not path:
        return f"<h2 style='font-family:sans-serif;color:#94a3b8;text-align:center;padding:4rem'>No {kind} HTML snapshot</h2>", 404
    body = path.read_bytes()
    body = _inject_script(body)
    return Response(body, mimetype="text/html")


def _proxy_live(agent: str, tid: str, kind: str, subpath: str):
    if kind not in ("baseline", "patched"):
        return "Not found", 404
    key = (agent, tid)
    with G["lock"]:
        sess = G["live_sessions"].get(key)
        if sess:
            sess["last_used"] = time.time()
    if not sess:
        return "Live session not available", 404

    base = sess["baseline_url"] if kind == "baseline" else sess["patched_url"]
    base = base.rstrip("/") + "/"
    rel = subpath.lstrip("/")
    target = urllib.parse.urljoin(base, rel)
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", errors="ignore")

    try:
        req = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
            status = resp.getcode()
            if ctype.startswith("text/html"):
                body = _inject_script(body)
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        ctype = e.headers.get("Content-Type", "text/plain") if e.headers else "text/plain"
        status = e.code
    except Exception as e:
        return f"Live proxy error: {e}", 502

    return Response(body, status=status, content_type=ctype)


@app.route("/live/<agent>/<tid>/<kind>/", defaults={"subpath": ""})
@app.route("/live/<agent>/<tid>/<kind>/<path:subpath>")
def serve_live(agent, tid, kind, subpath):
    return _proxy_live(agent, tid, kind, subpath)


@app.route("/admin")
def admin():
    tasks = G["tasks"]
    anns  = G["annotations"]
    annotators = sorted(set(
        a["annotator"] for rows in anns.values() for a in rows
    ))
    per_ann: dict = {}
    for name in annotators:
        done   = _annotator_done_count(tasks, anns, name)
        labels: dict = {}
        for rows in anns.values():
            for a in rows:
                if a["annotator"] == name:
                    labels[a["label"]] = labels.get(a["label"], 0) + 1
        per_ann[name] = {"done": done, "labels": labels}

    disagree = []
    for key, rows in anns.items():
        unique = set(r["label"] for r in rows)
        if len(unique) > 1:
            disagree.append({
                "agent": key[0], "template_id": key[1],
                "by": {r["annotator"]: r["label"] for r in rows},
            })

    return render_template_string(
        ADMIN_T,
        total=len(tasks), annotators=annotators,
        per_ann=per_ann, disagree=disagree,
        label_cfg=LABEL_CFG,
    )


@app.route("/export")
def export_csv():
    if not G["ann_file"].exists():
        return "No annotations yet", 404
    return send_file(G["ann_file"], as_attachment=True,
                     download_name="manual_annotations.csv")


# ─────────────────────────────────────────────────────────────────────────────
# HTML Templates
# ─────────────────────────────────────────────────────────────────────────────

_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Benchmark Annotator</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>"""

LOGIN_T = _HEAD + """
<body class="min-h-screen bg-slate-100 flex items-center justify-center">
  <div class="bg-white rounded-2xl shadow-lg p-10 w-full max-w-sm">
    <h1 class="text-2xl font-bold text-slate-800 mb-1">Benchmark Annotator</h1>
    <p class="text-slate-500 text-sm mb-6">Enter your name to start labelling patches.</p>
    {% if error %}
    <p class="text-red-500 text-sm mb-4">{{ error }}</p>
    {% endif %}
    <form method="POST">
      <input name="name" type="text" placeholder="Your name"
             class="w-full border border-slate-300 rounded-lg px-4 py-2 mb-4 text-slate-800
                    focus:outline-none focus:ring-2 focus:ring-blue-400" autofocus>
      <button class="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold
                     rounded-lg py-2 transition">Start annotating →</button>
    </form>
    <div class="mt-4 text-center">
      <a href="/admin" class="text-slate-400 text-xs hover:text-slate-600">Admin panel</a>
    </div>
  </div>
</body></html>"""


TASKS_T = _HEAD + """
<body class="bg-slate-50 min-h-screen">
  <nav class="bg-white border-b px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-4">
      <span class="font-bold text-slate-800">Benchmark Annotator</span>
      <span class="text-slate-400 text-sm">— {{ annotator }}</span>
    </div>
    <div class="flex items-center gap-4 text-sm">
      <div class="flex items-center gap-2">
        <div class="w-32 bg-slate-200 rounded-full h-2">
          <div class="bg-blue-500 h-2 rounded-full"
               style="width: {{ ((done / total * 100) | int) if total else 0 }}%"></div>
        </div>
        <span class="text-slate-600">{{ done }}/{{ total }}</span>
      </div>
      <a href="/admin" class="text-slate-500 hover:text-slate-800">Admin</a>
      <a href="/logout" class="text-red-400 hover:text-red-600">Logout</a>
    </div>
  </nav>

  <div class="max-w-6xl mx-auto px-6 py-6">
    <!-- Filters -->
    <form method="GET" class="flex flex-wrap gap-3 mb-6">
      <select name="agent" class="border border-slate-300 rounded-lg px-3 py-1.5 text-sm text-slate-700">
        <option value="">All agents</option>
        {% for a in agents %}
        <option value="{{ a }}" {% if af == a %}selected{% endif %}>{{ a }}</option>
        {% endfor %}
      </select>
      <select name="framework" class="border border-slate-300 rounded-lg px-3 py-1.5 text-sm text-slate-700">
        <option value="">All frameworks</option>
        {% for f in frameworks %}
        <option value="{{ f }}" {% if ff == f %}selected{% endif %}>{{ f }}</option>
        {% endfor %}
      </select>
      <select name="status" class="border border-slate-300 rounded-lg px-3 py-1.5 text-sm text-slate-700">
        <option value="pending" {% if sf == 'pending' %}selected{% endif %}>Pending</option>
        <option value="done"    {% if sf == 'done'    %}selected{% endif %}>Done</option>
        <option value="all"     {% if sf == 'all'     %}selected{% endif %}>All</option>
      </select>
      <button class="bg-slate-700 text-white px-4 py-1.5 rounded-lg text-sm hover:bg-slate-800">
        Filter
      </button>
      <a href="/tasks" class="text-slate-400 text-sm px-4 py-1.5 hover:text-slate-700">Reset</a>
    </form>

    <!-- Task grid -->
    {% if rows %}
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
      {% for r in rows %}
      <a href="/annotate/{{ r.agent }}/{{ r.template_id }}"
         class="bg-white rounded-xl border border-slate-200 shadow-sm hover:shadow-md
                transition p-4 flex flex-col gap-2 relative">
        {% if r.my_label %}
        <span class="absolute top-3 right-3 text-xs font-bold px-2 py-0.5 rounded-full"
              style="background-color: {{ label_cfg[r.my_label].bg }}22; color: {{ label_cfg[r.my_label].border }}">
          {{ label_cfg[r.my_label].icon }} {{ r.my_label }}
        </span>
        {% endif %}
        <div class="flex items-center gap-2 text-xs text-slate-500">
          <span class="bg-slate-100 px-2 py-0.5 rounded font-mono">{{ r.agent }}</span>
          <span class="bg-slate-100 px-2 py-0.5 rounded">{{ r.framework }}</span>
        </div>
        <div class="font-semibold text-slate-800">Template #{{ r.template_id }}</div>
        <div class="flex gap-3 text-xs text-slate-500 mt-auto">
          <span>V1: <b class="{% if r.v1_overall == 'YES' %}text-red-600{% elif r.v1_overall == 'NO' %}text-green-600{% else %}text-slate-400{% endif %}">{{ r.v1_overall or '—' }}</b></span>
          <span>V2: <b class="{% if r.v2_overall == 'YES' %}text-red-600{% elif r.v2_overall == 'NO' %}text-green-600{% else %}text-slate-400{% endif %}">{{ r.v2_overall or '—' }}</b></span>
          {% if r.all_count > 0 %}<span class="ml-auto text-slate-300">{{ r.all_count }} annotation{{ 's' if r.all_count > 1 }}</span>{% endif %}
        </div>
        {% if not r.has_html %}
        <div class="text-xs text-slate-300 italic">no snapshot</div>
        {% endif %}
      </a>
      {% endfor %}
    </div>
    {% else %}
    <div class="text-center text-slate-400 py-20">No tasks match the current filter.</div>
    {% endif %}
  </div>
</body></html>"""


ANNOTATE_T = _HEAD + """
<body style="display:flex;flex-direction:column;height:100vh;overflow:hidden;margin:0;background:#0f172a">

  <!-- Slim header bar -->
  <header style="height:38px;flex-shrink:0;background:#1e293b;border-bottom:1px solid #334155;
                 display:flex;align-items:center;padding:0 12px;gap:12px;color:#e2e8f0;font-size:13px">
    <a href="/tasks" style="color:#94a3b8;text-decoration:none;font-size:12px">← Tasks</a>
    <span style="background:#334155;padding:2px 8px;border-radius:4px;font-family:monospace;font-size:11px">{{ task.agent }}</span>
    <span style="color:#94a3b8">{{ task.framework }}</span>
    <span style="color:#64748b">#{{ task.template_id }}</span>

    <!-- Progress -->
    <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
      <div style="width:80px;height:4px;background:#334155;border-radius:2px">
        <div style="width:{{ ((done / total * 100)|int) if total else 0 }}%;height:4px;background:#3b82f6;border-radius:2px"></div>
      </div>
      <span style="color:#64748b;font-size:11px">{{ done }}/{{ total }}</span>
    </div>

    {% if prev_t %}
    <a href="/annotate/{{ prev_t.agent }}/{{ prev_t.template_id }}"
       style="color:#64748b;text-decoration:none;font-size:12px">← Prev</a>
    {% endif %}
    {% if next_t %}
    <a href="/annotate/{{ next_t.agent }}/{{ next_t.template_id }}"
       style="color:#64748b;text-decoration:none;font-size:12px">Next →</a>
    {% endif %}
    <a href="/logout" style="color:#f87171;text-decoration:none;font-size:11px">Logout</a>
  </header>

  <!-- Column labels -->
  <div style="display:flex;flex-shrink:0;height:26px;background:#1e293b;border-bottom:1px solid #334155">
    <div style="width:50%;display:flex;align-items:center;padding:0 12px;border-right:1px solid #334155;
                font-size:11px;font-weight:700;color:#4ade80;gap:6px">
      <span style="width:8px;height:8px;border-radius:50%;background:#4ade80;display:inline-block"></span>
      BASELINE
      <a href="{{ baseline_src }}" target="_blank"
         style="margin-left:auto;color:#475569;text-decoration:none;font-weight:400;font-size:10px">↗ full tab</a>
    </div>
    <div style="width:50%;display:flex;align-items:center;padding:0 12px;
                font-size:11px;font-weight:700;color:#fb923c;gap:6px">
      <span style="width:8px;height:8px;border-radius:50%;background:#fb923c;display:inline-block"></span>
      PATCHED — {{ task.agent }}
      <a href="{{ patched_src }}" target="_blank"
         style="margin-left:auto;color:#475569;text-decoration:none;font-weight:400;font-size:10px">↗ full tab</a>
    </div>
  </div>

  <!-- Main comparison — fills all remaining height above the footer -->
  <main style="display:flex;flex:1;min-height:0">
    <div style="width:50%;height:100%;border-right:1px solid #334155;display:flex;flex-direction:column">
      <div style="flex:7;min-height:0;background:#fff;position:relative">
        {% if has_bl_html %}
        <iframe src="{{ baseline_src }}"
                style="width:100%;height:100%;border:0" loading="lazy"></iframe>
        {% else %}
        <div style="display:flex;align-items:center;justify-content:center;height:100%;
                    color:#94a3b8;font-family:sans-serif;font-size:14px;background:#f8fafc">
          No HTML snapshot available
        </div>
        {% endif %}
      </div>
      <div style="flex:3;min-height:0;background:#0f172a;border-top:1px solid #334155;padding:8px;overflow-y:auto;font-family:monospace;font-size:11px">
        <div style="color:#94a3b8;margin-bottom:6px;font-weight:bold;text-transform:uppercase;font-size:10px;letter-spacing:1px">Baseline Console</div>
        <div id="console-baseline"></div>
      </div>
    </div>
    <div style="width:50%;height:100%;display:flex;flex-direction:column">
      <div style="flex:7;min-height:0;background:#fff;position:relative">
        {% if has_pt_html %}
        <iframe src="{{ patched_src }}"
                style="width:100%;height:100%;border:0" loading="lazy"></iframe>
        {% else %}
        <div style="display:flex;align-items:center;justify-content:center;height:100%;
                    color:#94a3b8;font-family:sans-serif;font-size:14px;background:#f8fafc">
          No HTML snapshot available
        </div>
        {% endif %}
      </div>
      <div style="flex:3;min-height:0;background:#0f172a;border-top:1px solid #334155;padding:8px;overflow-y:auto;font-family:monospace;font-size:11px">
        <div style="color:#94a3b8;margin-bottom:6px;font-weight:bold;text-transform:uppercase;font-size:10px;letter-spacing:1px">Patched Console</div>
        <div id="console-patched"></div>
      </div>
    </div>
  </main>

  <!-- Annotation footer -->
  <footer style="flex-shrink:0;background:#1e293b;border-top:2px solid #334155;padding:8px 14px">
    <!-- Metadata badges -->
    <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:7px;font-size:11px;color:#64748b">
      {% macro badge(label, val) %}
      <span>{{ label }}: <b style="color:{% if val == 'YES' %}#f87171{% elif val == 'NO' %}#4ade80{% else %}#475569{% endif %}">{{ val or '—' }}</b></span>
      {% endmacro %}
      {{ badge('V1', task.v1_overall) }}
      {{ badge('Structural', task.v2_structural) }}
      {{ badge('GPT', task.v2_gpt) }}
      {{ badge('Console', task.v2_console) }}
      {% if task.error %}<span style="color:#f87171">⚠ {{ task.error[:60] }}</span>{% endif %}
      {% if live_enabled %}<span style="color:#22c55e">LIVE localhost mode</span>{% else %}<span style="color:#f59e0b">Static snapshot fallback</span>{% endif %}
      {% for o in others %}
      <span style="margin-left:auto;color:#475569">{{ o.annotator }}:
        <b style="color:#e2e8f0">{{ o.label }}</b></span>
      {% endfor %}
    </div>

    <!-- Annotation form -->
    <form id="ann-form" method="POST" action="/submit"
          style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <input type="hidden" name="agent"       value="{{ task.agent }}">
      <input type="hidden" name="template_id" value="{{ task.template_id }}">
      <input type="hidden" name="elapsed"     id="elapsed" value="0">
      <input type="hidden" name="label"       id="label-input" value="">
      {% if next_u %}
      <input type="hidden" name="next_agent"  value="{{ next_u.agent }}">
      <input type="hidden" name="next_tid"    value="{{ next_u.template_id }}">
      {% endif %}

      <input name="notes" type="text" placeholder="Notes (optional)"
             style="flex:1;min-width:180px;background:#0f172a;border:1px solid #334155;border-radius:6px;
                    padding:5px 10px;font-size:13px;color:#e2e8f0;outline:none">

      {% for lbl in labels %}
      <button type="button" onclick="submitLabel('{{ lbl }}')"
              style="display:flex;align-items:center;gap:6px;padding:6px 14px;border-radius:8px;
                     font-weight:700;font-size:13px;color:#fff;border:2px solid {{ label_cfg[lbl].border }};
                     background:{{ label_cfg[lbl].bg }};cursor:pointer;
                     {% if existing and existing.label == lbl %}box-shadow:0 0 0 3px #fff,0 0 0 5px {{ label_cfg[lbl].bg }}{% endif %}">
        {{ label_cfg[lbl].icon }} {{ lbl }}
        <span style="font-size:10px;opacity:0.6;font-weight:400">[{{ label_cfg[lbl].key }}]</span>
      </button>
      {% endfor %}
    </form>

    <form method="POST" action="/live/kill" style="margin-top:6px;display:flex;gap:8px;align-items:center">
      <input type="hidden" name="agent" value="{{ task.agent }}">
      <input type="hidden" name="template_id" value="{{ task.template_id }}">
      {% if next_u %}
      <input type="hidden" name="next_agent" value="{{ next_u.agent }}">
      <input type="hidden" name="next_tid" value="{{ next_u.template_id }}">
      {% endif %}
      <button type="submit"
              style="padding:4px 10px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#94a3b8;font-size:11px;cursor:pointer">
        Kill Live Session For This Template
      </button>
      <span style="font-size:10px;color:#475569">Frees a slot and auto-starts the next queued live template.</span>
    </form>

    <div style="margin-top:5px;font-size:10px;color:#334155">
      {% if existing %}Your label: <b style="color:#94a3b8">{{ existing.label }}</b>{% if existing.notes %} — {{ existing.notes }}{% endif %} &nbsp;·&nbsp; {% endif %}
      Keys: {% for lbl in labels %}{{ label_cfg[lbl].key }} {{ lbl }} &nbsp; {% endfor %} |&nbsp; ← → navigate
    </div>
  </footer>

  <script>
    const startTime = Date.now();

    window.addEventListener('message', function(e) {
      if (e.data && e.data.type === 'console_error') {
        const div = document.getElementById('console-' + e.data.kind);
        if (div) {
          const entry = document.createElement('div');
          entry.style.color = '#ef4444';
          entry.style.marginBottom = '4px';
          entry.style.borderBottom = '1px solid #1e293b';
          entry.style.paddingBottom = '4px';
          entry.style.wordBreak = 'break-all';
          entry.textContent = e.data.msg;
          div.appendChild(entry);
          div.parentElement.scrollTop = div.parentElement.scrollHeight;
        }
      }
    });

    function submitLabel(lbl) {
      document.getElementById('label-input').value = lbl;
      document.getElementById('elapsed').value = Math.round((Date.now() - startTime) / 1000);
      document.getElementById('ann-form').submit();
    }

    document.addEventListener('keydown', function(e) {
      if (['INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;
      {% for lbl in labels %}
      if (e.key === '{{ label_cfg[lbl].key }}') submitLabel('{{ lbl }}');
      {% endfor %}
      if (e.key === 'ArrowLeft') {
        {% if prev_t %}window.location = '/annotate/{{ prev_t.agent }}/{{ prev_t.template_id }}';{% endif %}
      } else if (e.key === 'ArrowRight') {
        {% if next_t %}window.location = '/annotate/{{ next_t.agent }}/{{ next_t.template_id }}';{% endif %}
      }
    });
  </script>
</body></html>"""


ADMIN_T = _HEAD + """
<body class="bg-slate-50 min-h-screen">
  <nav class="bg-white border-b px-6 py-3 flex items-center justify-between">
    <div class="font-bold text-slate-800">Admin Panel</div>
    <div class="flex gap-4 text-sm">
      <a href="/tasks" class="text-blue-600 hover:underline">← Back to tasks</a>
      <a href="/export" class="text-green-600 hover:underline">⬇ Export CSV</a>
    </div>
  </nav>

  <div class="max-w-4xl mx-auto px-6 py-8">
    <h2 class="text-xl font-bold text-slate-800 mb-6">Annotation Progress</h2>

    <!-- Per-annotator stats -->
    {% if per_ann %}
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-8">
      {% for name, s in per_ann.items() %}
      <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
        <div class="font-semibold text-slate-800 mb-2">{{ name }}</div>
        <div class="flex items-center gap-2 mb-3">
          <div class="flex-1 bg-slate-200 rounded-full h-2">
            <div class="bg-blue-500 h-2 rounded-full"
                 style="width: {{ ((s.done / total * 100)|int) if total else 0 }}%"></div>
          </div>
          <span class="text-sm text-slate-600">{{ s.done }}/{{ total }}</span>
        </div>
        <div class="flex flex-wrap gap-2 text-xs">
          {% for lbl, cnt in s.labels.items() %}
          <span class="px-2 py-0.5 rounded-full font-medium"
                style="background-color: {{ label_cfg[lbl].bg }}22; color: {{ label_cfg[lbl].border }}">
            {{ label_cfg[lbl].icon }} {{ lbl }}: {{ cnt }}
          </span>
          {% endfor %}
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <p class="text-slate-400 mb-8">No annotations yet.</p>
    {% endif %}

    <!-- Disagreements -->
    {% if disagree %}
    <h2 class="text-xl font-bold text-slate-800 mb-4">Disagreements ({{ disagree|length }})</h2>
    <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden mb-8">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-slate-600 text-xs uppercase tracking-wide">
          <tr>
            <th class="px-4 py-3 text-left">Agent</th>
            <th class="px-4 py-3 text-left">Template</th>
            <th class="px-4 py-3 text-left">Labels</th>
            <th class="px-4 py-3"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {% for d in disagree %}
          <tr>
            <td class="px-4 py-3 font-mono text-xs text-slate-500">{{ d.agent }}</td>
            <td class="px-4 py-3 text-slate-700">#{{ d.template_id }}</td>
            <td class="px-4 py-3">
              {% for ann_name, lbl in d.by.items() %}
              <span class="mr-3 text-xs">
                <span class="text-slate-500">{{ ann_name }}:</span>
                <b style="color: {{ label_cfg[lbl].bg }}">{{ lbl }}</b>
              </span>
              {% endfor %}
            </td>
            <td class="px-4 py-3">
              <a href="/annotate/{{ d.agent }}/{{ d.template_id }}"
                 class="text-blue-500 hover:underline text-xs">Review →</a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% endif %}

    <p class="text-slate-400 text-sm">Total tasks: {{ total }}</p>
  </div>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    here = Path(__file__).parent
    repo_root = here.parents[1]

    parser = argparse.ArgumentParser(description="Manual annotation server")
    parser.add_argument("--port",          type=int,  default=7000)
    parser.add_argument("--patches",       type=Path, default=repo_root / "code_patches",
                        help="Path to code_patches/ dir containing results_* subdirs")
    parser.add_argument("--templates-csv", type=Path, default=repo_root / "harness" / "SAMPLE" / "input.csv",
                        help="Harness input.csv for framework names")
    parser.add_argument("--results",       type=Path, default=here / "results",
                        help="Path to results/ dir for eval overlay and annotation output")
    parser.add_argument("--out",           type=Path, default=None,
                        help="Annotations CSV output path (default: <results>/manual_annotations.csv)")
    parser.add_argument("--max-live-sessions", type=int, default=2,
                        help="Max live task sessions kept running at once")
    parser.add_argument("--live-ttl-sec", type=int, default=900,
                        help="Idle TTL in seconds before a live session is evicted")
    parser.add_argument("--prewarm-count", type=int, default=1,
                        help="How many upcoming tasks to prewarm in background")
    parser.add_argument("--prewarm-workers", type=int, default=1,
                        help="Background prewarm worker threads")
    args = parser.parse_args()

    patches_dir  = args.patches.resolve()
    results_dir  = args.results.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    if not patches_dir.exists():
        sys.exit(f"Patches dir not found: {patches_dir}")

    ann_file     = args.out or results_dir / "manual_annotations.csv"
    template_map = _load_template_map(args.templates_csv.resolve())
    template_info_map = load_template_map()
    tasks        = _load_tasks(patches_dir, results_dir, template_map)
    anns         = _load_annotations(ann_file)

    eval_csv = results_dir / "eval_results.csv"

    G.update(
        results_dir=results_dir,
        ann_file=ann_file,
        patches_dir=patches_dir,
        template_info_map={str(k): v for k, v in template_info_map.items()},
        tasks=tasks,
        annotations=anns,
        live_sessions={},
        live_inflight=set(),
        live_fail_count={},
        live_fail_until={},
        queue_idx=0,
        max_live_sessions=max(1, args.max_live_sessions),
        live_ttl_sec=max(60, args.live_ttl_sec),
        prewarm_count=max(0, args.prewarm_count),
        prewarm_pool=ThreadPoolExecutor(max_workers=max(1, args.prewarm_workers)),
        lock=threading.Lock(),
    )
    atexit.register(_cleanup_live_sessions)
    atexit.register(lambda: G.get("prewarm_pool") and G["prewarm_pool"].shutdown(wait=False))

    print(f"\n  Benchmark Annotator", flush=True)
    print(f"  Tasks loaded : {len(tasks)}", flush=True)
    print(f"  Eval results : {'present' if eval_csv.exists() else 'not found — running without scores'}", flush=True)
    print(f"  Annotations  : {sum(len(v) for v in anns.values())} existing", flush=True)
    print(f"  Patches dir  : {patches_dir}", flush=True)
    print(f"  Results dir  : {results_dir}", flush=True)
    print(f"  Output CSV   : {ann_file}", flush=True)
    print(f"  Live cache   : max={G['max_live_sessions']} ttl={G['live_ttl_sec']}s prewarm={G['prewarm_count']} workers={max(1, args.prewarm_workers)}", flush=True)
    print(f"  Startup warm : target {G['max_live_sessions']} live sessions", flush=True)
    print(f"\n  Open http://localhost:{args.port} in any browser on this machine.\n", flush=True)

    # Immediately begin warming up to max_live_sessions in the background.
    _fill_live_pool()

    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
