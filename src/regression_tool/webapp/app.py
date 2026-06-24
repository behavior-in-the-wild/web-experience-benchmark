"""
Flask web application for webpage comparison report generation.

Accepts two HTML inputs (file uploads or URLs) and runs
create_comparison_report.py to produce a visual diff report.
Streams live progress via SSE.

Usage:
    python webapp/app.py [--port 5000]
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("comparison-webapp")

app = Flask(__name__)

REPORT_SCRIPT = str(Path(__file__).resolve().parent.parent / "create_comparison_report.py")
SCREENSHOT_DIFF_SCRIPT = str(Path(__file__).resolve().parent.parent / "screenshot_diff.py")

JOBS: dict[str, dict] = {}
BULK_JOBS: dict[str, dict] = {}
SCREENSHOT_JOBS: dict[str, dict] = {}
_print_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────

def _send(q: queue.Queue, event: str, data: dict) -> None:
    q.put({"event": event, "data": data})


# Step patterns for report script output
_REPORT_STEP_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    (re.compile(r"Taking full-page screenshots"),                 "Capturing full-page screenshots"),
    (re.compile(r"Building original template.s visual tree"),     "Building original visual tree"),
    (re.compile(r"Detecting original template.s section nodes"),  "Detecting original section nodes"),
    (re.compile(r"Building generated template.s visual tree"),    "Building generated visual tree"),
    (re.compile(r"Detecting generated template.s section nodes"), "Detecting generated section nodes"),
    (re.compile(r"All reports generated"),                        "All reports generated"),
]
_REPORT_FIXED_BEFORE = 5
_REPORT_FIXED_AFTER = 1


def _stream_report(cmd: list[str], label: str, q: queue.Queue, debug: bool = False) -> tuple[int, str]:
    """Run the report script, parse stdout, and emit SSE report_step events."""
    matching_types_seen: list[str] = []
    llm_diffs_seen: list[str] = []
    llm_starts_seen: int = 0

    def _total() -> int:
        return (_REPORT_FIXED_BEFORE
                + max(len(matching_types_seen), 1)
                + llm_starts_seen
                + len(llm_diffs_seen)
                + _REPORT_FIXED_AFTER)

    steps_done = 0

    def _emit(display: str) -> None:
        nonlocal steps_done
        steps_done += 1
        _send(q, "report_step", {"label": display, "done": steps_done, "total": _total()})

    log.info("START  %s — %s", label, " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []

    _on_matching  = re.compile(r"Running (\w+) matching \.\.\.", re.IGNORECASE)
    _on_llm_start = re.compile(r"Running LLM-based diff generation for (\w+) matching", re.IGNORECASE)
    _on_llm_done  = re.compile(r"LLM diff generation complete for (\w+) matching", re.IGNORECASE)

    for line in proc.stdout:
        lines.append(line)
        with _print_lock:
            sys.stderr.write(f"[{label}] {line}")
            sys.stderr.flush()
        stripped = line.strip()
        if debug and stripped:
            _send(q, "log_line", {"text": stripped})

        m = _on_llm_start.search(stripped)
        if m:
            llm_starts_seen += 1
            _emit("Getting VLM based style differences")
            continue

        m = _on_matching.search(stripped)
        if m:
            mt = m.group(1).upper()
            matching_types_seen.append(mt)
            _emit(f"Running {mt} matching")
            continue

        m = _on_llm_done.search(stripped)
        if m:
            mt = m.group(1).upper()
            llm_diffs_seen.append(mt)
            _emit(f"LLM diff generation complete ({mt})")
            continue

        for pattern, display in _REPORT_STEP_PATTERNS:
            if pattern.search(stripped):
                _emit(display)
                break

    proc.wait()
    output = "".join(lines)
    if proc.returncode != 0:
        log.error("FAIL   %s — exit code %d", label, proc.returncode)
    else:
        log.info("DONE   %s", label)
    return proc.returncode, output


def _build_report_cmd(output_dir: str, opts: dict) -> list[str]:
    """Build the create_comparison_report.py command from user options."""
    cmd = ["python", REPORT_SCRIPT, "--output-dir", output_dir]

    # Input: URLs take precedence over file paths
    if opts.get("original_url"):
        cmd += ["--original-url", opts["original_url"]]
    elif opts.get("original_html_path"):
        cmd += ["--original-html-file-path", opts["original_html_path"]]

    if opts.get("generated_url"):
        cmd += ["--generated-url", opts["generated_url"]]
    elif opts.get("generated_html_path"):
        cmd += ["--generated-html-file-path", opts["generated_html_path"]]

    # Matching types
    matching_types = opts.get("matching_types") or ["heuristic"]
    cmd += ["--matching-types"] + matching_types

    # Optional numeric params
    cmd += ["--thresh-height", str(opts["thresh_height"] if opts.get("thresh_height") is not None else -1)]
    if opts.get("max_leaves") is not None:
        cmd += ["--max-leaves", str(opts["max_leaves"])]

    # Flags
    if opts.get("run_llm_diffs"):
        cmd.append("--run-llm-diffs")
    if opts.get("ai_provider"):
        cmd += ["--ai-provider", opts["ai_provider"]]
    if opts.get("llm_concurrency") is not None:
        cmd += ["--llm-concurrency", str(opts["llm_concurrency"])]
    if opts.get("use_fragment_sectioning"):
        cmd.append("--use-fragment-sectioning")
    if opts.get("debug"):
        cmd.append("--debug")

    return cmd


def _parse_opts(form) -> dict:
    """Extract report options from a Flask request form."""
    opts: dict = {}

    opts["original_url"] = form.get("original_url", "").strip() or None
    opts["generated_url"] = form.get("generated_url", "").strip() or None

    # max_leaves / thresh_height
    ml = form.get("max_leaves", "").strip()
    opts["max_leaves"] = int(ml) if ml else None
    th = form.get("thresh_height", "").strip()
    opts["thresh_height"] = int(th) if th else None

    # matching types
    mt_raw = form.getlist("matching_types")
    if not mt_raw:
        mt_raw = [form.get("matching_types", "heuristic")]
    mt = []
    for v in mt_raw:
        mt.extend(x.strip() for x in v.split(",") if x.strip())
    opts["matching_types"] = mt if mt else ["heuristic"]

    opts["run_llm_diffs"] = form.get("run_llm_diffs") == "1"
    opts["ai_provider"] = form.get("ai_provider", "").strip() or "gpt41"

    lc = form.get("llm_concurrency", "").strip()
    opts["llm_concurrency"] = int(lc) if lc else 5

    opts["use_fragment_sectioning"] = form.get("use_fragment_sectioning") == "1"
    opts["debug"] = form.get("debug") == "1"

    bc = form.get("bulk_concurrency", "").strip()
    opts["bulk_concurrency"] = max(1, min(int(bc), 32)) if bc else 4

    return opts


def _process_report_job(
    job_id: str,
    output_dir: str,
    opts: dict,
    q: queue.Queue,
    work_dir: str,
) -> None:
    """Background worker: runs create_comparison_report.py and emits SSE events."""
    job = JOBS[job_id]
    log.info("=" * 60)
    log.info("JOB %s — started", job_id)
    try:
        _send(q, "phase", {"phase": "report", "message": "Generating comparison report…"})
        t0 = time.time()

        cmd = _build_report_cmd(output_dir, opts)
        rc, output = _stream_report(cmd, f"report:{job_id}", q, debug=opts.get("debug", False))
        elapsed = time.time() - t0

        if rc != 0:
            _send(q, "error", {"message": f"Report generation failed (exit {rc}).\n{output[-3000:]}"})
            job["status"] = "error"
            return

        _send(q, "phase_done", {"phase": "report", "elapsed": round(elapsed, 2)})
        job["report_time"] = round(elapsed, 2)
        job["status"] = "done"

        # Collect generated report files
        matching_types = opts.get("matching_types") or ["heuristic"]
        report_paths: dict[str, str] = {}
        for mt in matching_types:
            rpts = glob.glob(os.path.join(output_dir, f"*_conversion_report_{mt}.html"))
            if rpts:
                report_paths[mt] = rpts[0]

        _send(q, "done", {
            "job_id": job_id,
            "report_time": job["report_time"],
            "report_types": list(report_paths.keys()),
        })

    except Exception as exc:
        log.error("JOB %s — unhandled exception:\n%s", job_id, traceback.format_exc())
        job["status"] = "error"
        _send(q, "error", {"message": str(exc)})
    finally:
        _send(q, "end", {})


# ── Bulk run helpers ──────────────────────────────────────────────────────

def _parse_scores_from_report(html_path: str) -> dict:
    """Parse Leaf IoU, Section IoU, VLM CSS Similarity from a report HTML."""
    try:
        with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        scores: dict = {}
        m = re.search(r'id="headerLeafIou">([\d.]+)<', content)
        if m:
            scores["leaf_iou"] = float(m.group(1))
        m = re.search(r'id="headerSectionIou">([\d.]+)<', content)
        if m:
            scores["section_iou"] = float(m.group(1))
        m = re.search(r'<div class="v">([\d.]+)</div>\s*<div class="l">VLM CSS Similarity</div>', content)
        if m:
            scores["vlm_similarity"] = float(m.group(1))
        return scores
    except Exception:
        return {}


def _process_bulk_pair(
    pair_id: str,
    orig_path: str,
    gen_path: str,
    opts: dict,
    output_dir: str,
    pair_name: str,
) -> dict:
    """Run one pair comparison and return a result dict with parsed scores."""
    pair_opts = dict(opts)
    pair_opts["original_html_path"] = orig_path
    pair_opts["generated_html_path"] = gen_path
    pair_opts.pop("original_url", None)
    pair_opts.pop("generated_url", None)

    cmd = _build_report_cmd(output_dir, pair_opts)
    _dummy_q: queue.Queue = queue.Queue()
    rc, _ = _stream_report(cmd, f"bulk:{pair_name[:28]}", _dummy_q, debug=False)

    result: dict = {
        "pair_id": pair_id,
        "name": pair_name,
        "status": "done" if rc == 0 else "error",
        "scores": {},
        "report_types": [],
    }
    if rc == 0:
        for mt in (opts.get("matching_types") or ["heuristic"]):
            rpts = glob.glob(os.path.join(output_dir, f"*_conversion_report_{mt}.html"))
            if rpts:
                result["report_types"].append(mt)
                s = _parse_scores_from_report(rpts[0])
                if s:
                    result["scores"][mt] = s
    return result


def _process_bulk_job(
    bulk_id: str,
    pairs: list,
    opts: dict,
    base_work_dir: str,
) -> None:
    """Background: run all pairs with a thread pool, emit SSE bulk_progress events."""
    bulk_job = BULK_JOBS[bulk_id]
    q = bulk_job["queue"]
    results_list = bulk_job["results"]
    total = len(pairs)

    _send(q, "bulk_start", {"total": total})
    outputs_dir = os.path.join(base_work_dir, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)

    def run_one(pair):
        pair_name, orig_path, gen_path = pair
        pair_id = uuid.uuid4().hex[:8]
        pair_out = os.path.join(outputs_dir, pair_id)
        os.makedirs(pair_out, exist_ok=True)
        return _process_bulk_pair(pair_id, orig_path, gen_path, opts, pair_out, pair_name)

    done_count = 0
    concurrency = opts.get("bulk_concurrency", 4)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_pair = {executor.submit(run_one, p): p for p in pairs}
        for future in as_completed(future_to_pair):
            try:
                result = future.result()
            except Exception:
                pair_name = future_to_pair[future][0]
                log.error("Bulk pair %s failed:\n%s", pair_name, traceback.format_exc())
                result = {
                    "pair_id": "",
                    "name": pair_name,
                    "status": "error",
                    "scores": {},
                    "report_types": [],
                }
            results_list.append(result)
            done_count += 1
            _send(q, "bulk_progress", {"done": done_count, "total": total, "result": result})

    bulk_job["status"] = "done"
    _send(q, "bulk_done", {"bulk_id": bulk_id, "total": total})
    _send(q, "end", {})


# ── Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/compare", methods=["POST"])
def compare():
    """
    Accepts:
      - original_url / generated_url  (text fields), OR
      - original_html / generated_html (file uploads — plain .html or .zip of html+_files)
    Plus report options from the form.
    """
    opts = _parse_opts(request.form)

    job_id = uuid.uuid4().hex[:12]
    work_dir = tempfile.mkdtemp(prefix=f"cmp_{job_id}_")
    output_dir = os.path.join(work_dir, "output")
    os.makedirs(output_dir)

    # ── Handle file uploads ────────────────────────────────────────
    orig_file = request.files.get("original_html")
    gen_file = request.files.get("generated_html")

    def _save_input(uploaded_file, label: str) -> str | None:
        """Save an uploaded .html or .zip (html + _files companion) and return the .html path."""
        if not uploaded_file or not uploaded_file.filename:
            return None
        fname = uploaded_file.filename
        dest_dir = os.path.join(work_dir, label)
        os.makedirs(dest_dir, exist_ok=True)
        if fname.lower().endswith(".zip"):
            zip_path = os.path.join(dest_dir, "upload.zip")
            uploaded_file.save(zip_path)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(dest_dir)
            # Find the top-level .html file
            html_files = [
                os.path.join(dest_dir, n)
                for n in os.listdir(dest_dir)
                if n.lower().endswith(".html")
            ]
            return html_files[0] if html_files else None
        else:
            save_path = os.path.join(dest_dir, fname)
            uploaded_file.save(save_path)
            return save_path

    if not opts.get("original_url"):
        opts["original_html_path"] = _save_input(orig_file, "original")
    if not opts.get("generated_url"):
        opts["generated_html_path"] = _save_input(gen_file, "generated")

    # Validate: need at least one input per side
    if not opts.get("original_url") and not opts.get("original_html_path"):
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": "Provide an original HTML file or URL"}), 400
    if not opts.get("generated_url") and not opts.get("generated_html_path"):
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": "Provide a generated HTML file or URL"}), 400

    event_queue: queue.Queue = queue.Queue()
    job = {
        "id": job_id,
        "status": "running",
        "work_dir": work_dir,
        "output_dir": output_dir,
        "queue": event_queue,
        "opts": opts,
    }
    JOBS[job_id] = job

    t = threading.Thread(
        target=_process_report_job,
        args=(job_id, output_dir, opts, event_queue, work_dir),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/events/<job_id>")
def events(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def stream():
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=120)
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
                continue
            evt = msg["event"]
            data = json.dumps(msg["data"])
            yield f"event: {evt}\ndata: {data}\n\n"
            if evt == "end":
                break

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/view/<job_id>/report")
def view_report(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    mt = request.args.get("type", "heuristic")
    output_dir = job.get("output_dir", "")
    rpts = glob.glob(os.path.join(output_dir, f"*_conversion_report_{mt}.html"))
    if not rpts:
        return jsonify({"error": f"Report ({mt}) not found"}), 404
    return send_file(rpts[0], mimetype="text/html")


@app.route("/cleanup/<job_id>", methods=["POST"])
def cleanup(job_id: str):
    job = JOBS.pop(job_id, None)
    if job and os.path.isdir(job.get("work_dir", "")):
        shutil.rmtree(job["work_dir"], ignore_errors=True)
    return jsonify({"ok": True})


# ── Bulk routes ───────────────────────────────────────────────────────────

@app.route("/compare_bulk", methods=["POST"])
def compare_bulk():
    opts = _parse_opts(request.form)

    orig_bundle = request.files.get("original_bundle")
    gen_bundle  = request.files.get("generated_bundle")
    if not orig_bundle or not orig_bundle.filename:
        return jsonify({"error": "Provide original HTML bundle (ZIP)"}), 400
    if not gen_bundle or not gen_bundle.filename:
        return jsonify({"error": "Provide generated HTML bundle (ZIP)"}), 400

    bulk_id  = uuid.uuid4().hex[:12]
    work_dir = tempfile.mkdtemp(prefix=f"bulk_{bulk_id}_")
    orig_dir = os.path.join(work_dir, "originals")
    gen_dir  = os.path.join(work_dir, "generated")
    os.makedirs(orig_dir)
    os.makedirs(gen_dir)

    orig_zip_path = os.path.join(work_dir, "orig.zip")
    gen_zip_path  = os.path.join(work_dir, "gen.zip")
    orig_bundle.save(orig_zip_path)
    gen_bundle.save(gen_zip_path)

    try:
        with zipfile.ZipFile(orig_zip_path, "r") as zf:
            zf.extractall(orig_dir)
        with zipfile.ZipFile(gen_zip_path, "r") as zf:
            zf.extractall(gen_dir)
    except zipfile.BadZipFile as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": f"Invalid ZIP file: {exc}"}), 400

    def _collect(base: str) -> dict:
        found: dict = {}
        for root, _, fnames in os.walk(base):
            for fn in fnames:
                if fn.lower().endswith(".html") and not fn.startswith("._"):
                    found[fn] = os.path.join(root, fn)
        return found

    orig_files    = _collect(orig_dir)
    gen_files     = _collect(gen_dir)
    matched_names = sorted(set(orig_files) & set(gen_files))

    if not matched_names:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": "No matching HTML filenames found in both ZIPs"}), 400

    pairs = [(name, orig_files[name], gen_files[name]) for name in matched_names[:500]]

    event_queue: queue.Queue = queue.Queue()
    bulk_job = {
        "id": bulk_id,
        "status": "running",
        "work_dir": work_dir,
        "queue": event_queue,
        "results": [],
        "opts": opts,
    }
    BULK_JOBS[bulk_id] = bulk_job

    threading.Thread(
        target=_process_bulk_job,
        args=(bulk_id, pairs, opts, work_dir),
        daemon=True,
    ).start()

    return jsonify({"bulk_id": bulk_id, "total_pairs": len(pairs)})


@app.route("/bulk_events/<bulk_id>")
def bulk_events(bulk_id: str):
    job = BULK_JOBS.get(bulk_id)
    if not job:
        return jsonify({"error": "Bulk job not found"}), 404

    def stream():
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=120)
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
                continue
            evt  = msg["event"]
            data = json.dumps(msg["data"])
            yield f"event: {evt}\ndata: {data}\n\n"
            if evt == "end":
                break

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/bulk_status/<bulk_id>")
def bulk_status(bulk_id: str):
    job = BULK_JOBS.get(bulk_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"status": job["status"], "results": job["results"]})


@app.route("/view_bulk/<bulk_id>/<pair_id>/report")
def view_bulk_report(bulk_id: str, pair_id: str):
    job = BULK_JOBS.get(bulk_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    mt       = request.args.get("type", "heuristic")
    pair_dir = os.path.join(job["work_dir"], "outputs", pair_id)
    rpts     = glob.glob(os.path.join(pair_dir, f"*_conversion_report_{mt}.html"))
    if not rpts:
        return jsonify({"error": "Report not found"}), 404
    return send_file(rpts[0], mimetype="text/html")


@app.route("/download_bulk/<bulk_id>")
def download_bulk(bulk_id: str):
    job = BULK_JOBS.get(bulk_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": "Job not complete yet"}), 400

    work_dir    = job["work_dir"]
    zip_path    = os.path.join(work_dir, "reports_bundle.zip")
    outputs_dir = os.path.join(work_dir, "outputs")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for result in job["results"]:
            if result.get("status") != "done":
                continue
            pair_id   = result.get("pair_id", "")
            pair_name = result.get("name", pair_id)
            pair_dir  = os.path.join(outputs_dir, pair_id)
            if not os.path.isdir(pair_dir):
                continue
            folder = os.path.splitext(pair_name)[0]
            for fn in os.listdir(pair_dir):
                if fn.endswith(".html"):
                    zf.write(os.path.join(pair_dir, fn), f"{folder}/{fn}")

    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="comparison_reports.zip",
    )


@app.route("/cleanup_bulk/<bulk_id>", methods=["POST"])
def cleanup_bulk(bulk_id: str):
    job = BULK_JOBS.pop(bulk_id, None)
    if job and os.path.isdir(job.get("work_dir", "")):
        shutil.rmtree(job["work_dir"], ignore_errors=True)
    return jsonify({"ok": True})


# ── Screenshot diff helpers ───────────────────────────────────────────────

_SDIFF_STAGES = {
    "rendering_orig": "Rendering original HTML…",
    "rendering_gen":  "Rendering generated HTML…",
    "computing_diff": "Computing pixel diff…",
    "building_report": "Building diff report…",
}


def _run_screenshot_diff_cmd(cmd: list[str], q: queue.Queue) -> tuple[int, dict]:
    """Run screenshot_diff.py as a subprocess, parse structured stdout, emit SSE events."""
    steps_done = 0
    total_steps = len(_SDIFF_STAGES)
    result_data: dict = {}

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        with _print_lock:
            sys.stderr.write(f"[sdiff] {line}")
            sys.stderr.flush()
        stripped = line.strip()
        if stripped.startswith("[sdiff:done]"):
            try:
                result_data = json.loads(stripped[len("[sdiff:done]"):].strip())
            except Exception:
                pass
        elif stripped.startswith("[sdiff:"):
            stage = stripped[len("[sdiff:"):].rstrip("]")
            label = _SDIFF_STAGES.get(stage, stage)
            steps_done += 1
            _send(q, "screenshot_step", {
                "label": label,
                "done": steps_done,
                "total": total_steps,
            })
    proc.wait()
    return proc.returncode, result_data


def _process_screenshot_diff_job(
    job_id: str,
    html_a: str,
    html_b: str,
    output_dir: str,
    viewport_width: int,
    q: queue.Queue,
) -> None:
    """Background worker: runs screenshot_diff.py and emits SSE events."""
    job = SCREENSHOT_JOBS[job_id]
    try:
        _send(q, "phase", {"phase": "screenshot_diff", "message": "Running screenshot diff…"})
        t0 = time.time()

        cmd = [
            "python", SCREENSHOT_DIFF_SCRIPT,
            "--html-a", html_a,
            "--html-b", html_b,
            "--output-dir", output_dir,
            "--viewport-width", str(viewport_width),
        ]
        rc, result_data = _run_screenshot_diff_cmd(cmd, q)
        elapsed = time.time() - t0

        if rc != 0:
            _send(q, "error", {"message": f"Screenshot diff failed (exit {rc})."})
            job["status"] = "error"
            return

        job["status"] = "done"
        job["result"] = result_data
        _send(q, "phase_done", {"phase": "screenshot_diff", "elapsed": round(elapsed, 2)})
        _send(q, "screenshot_done", {
            "job_id": job_id,
            "bbox_count": result_data.get("bbox_count", 0),
            "bbox_fraction": result_data.get("bbox_fraction", 0.0),
            "elapsed": round(elapsed, 2),
        })
    except Exception as exc:
        log.error("Screenshot diff job %s failed:\n%s", job_id, traceback.format_exc())
        job["status"] = "error"
        _send(q, "error", {"message": str(exc)})
    finally:
        _send(q, "end", {})


# ── Screenshot diff routes ────────────────────────────────────────────────

@app.route("/compare_screenshot", methods=["POST"])
def compare_screenshot():
    """Accept two HTML inputs and run a screenshot-only visual diff."""
    job_id = uuid.uuid4().hex[:12]
    work_dir = tempfile.mkdtemp(prefix=f"sdiff_{job_id}_")
    output_dir = os.path.join(work_dir, "output")
    os.makedirs(output_dir)

    orig_file = request.files.get("original_html")
    gen_file  = request.files.get("generated_html")

    def _save_input(uploaded_file, label: str) -> str | None:
        if not uploaded_file or not uploaded_file.filename:
            return None
        fname = uploaded_file.filename
        dest_dir = os.path.join(work_dir, label)
        os.makedirs(dest_dir, exist_ok=True)
        if fname.lower().endswith(".zip"):
            zip_path = os.path.join(dest_dir, "upload.zip")
            uploaded_file.save(zip_path)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(dest_dir)
            html_files = [
                os.path.join(dest_dir, n)
                for n in os.listdir(dest_dir)
                if n.lower().endswith(".html")
            ]
            return html_files[0] if html_files else None
        else:
            save_path = os.path.join(dest_dir, fname)
            uploaded_file.save(save_path)
            return save_path

    original_url = request.form.get("original_url", "").strip() or None
    generated_url = request.form.get("generated_url", "").strip() or None

    html_a = original_url
    if not html_a:
        html_a = _save_input(orig_file, "original")
    html_b = generated_url
    if not html_b:
        html_b = _save_input(gen_file, "generated")

    if not html_a:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": "Provide an original HTML file or URL"}), 400
    if not html_b:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": "Provide a generated HTML file or URL"}), 400

    vw_raw = request.form.get("viewport_width", "").strip()
    viewport_width = int(vw_raw) if vw_raw.isdigit() else 1280

    event_queue: queue.Queue = queue.Queue()
    job = {
        "id": job_id,
        "status": "running",
        "work_dir": work_dir,
        "output_dir": output_dir,
        "queue": event_queue,
        "result": {},
    }
    SCREENSHOT_JOBS[job_id] = job

    threading.Thread(
        target=_process_screenshot_diff_job,
        args=(job_id, html_a, html_b, output_dir, viewport_width, event_queue),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.route("/screenshot_events/<job_id>")
def screenshot_events(job_id: str):
    job = SCREENSHOT_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def stream():
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=120)
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
                continue
            evt  = msg["event"]
            data = json.dumps(msg["data"])
            yield f"event: {evt}\ndata: {data}\n\n"
            if evt == "end":
                break

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/view_screenshot/<job_id>/report")
def view_screenshot_report(job_id: str):
    job = SCREENSHOT_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    report = os.path.join(job.get("output_dir", ""), "diff_report.html")
    if not os.path.exists(report):
        return jsonify({"error": "Report not found"}), 404
    return send_file(report, mimetype="text/html")


@app.route("/cleanup_screenshot/<job_id>", methods=["POST"])
def cleanup_screenshot(job_id: str):
    job = SCREENSHOT_JOBS.pop(job_id, None)
    if job and os.path.isdir(job.get("work_dir", "")):
        shutil.rmtree(job["work_dir"], ignore_errors=True)
    return jsonify({"ok": True})


# ── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
