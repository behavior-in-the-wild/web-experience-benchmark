#!/usr/bin/env python3
"""
Visual regression viewer.

Usage:
  python harness/visual_viewer.py --results-dir harness/out/<run>/results
  python harness/visual_viewer.py --results-dir harness/out/<run>/results --port 5050
"""
import argparse
import json
from pathlib import Path
from flask import Flask, abort, render_template_string, send_file

app = Flask(__name__)
RESULTS_DIR: Path = Path()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_jobs():
    jobs = []
    if not RESULTS_DIR.is_dir():
        return jobs
    for job_dir in sorted(RESULTS_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        visual_json = job_dir / "visual.json"
        visual = None
        if visual_json.exists():
            try:
                visual = json.loads(visual_json.read_text())
            except Exception:
                pass
        jobs.append({"label": job_dir.name, "dir": job_dir, "visual": visual})
    return jobs


def job_status(visual):
    if visual is None:
        return "unknown"
    return "fail" if visual.get("overall_regression") else "pass"


def img_url(job_dir: Path, filename: str):
    p = job_dir / filename
    return f"/image/{job_dir.name}/{filename}" if p.exists() else None


def first_img_url(job_dir: Path, *filenames: str):
    for filename in filenames:
        url = img_url(job_dir, filename)
        if url:
            return url
    return None


# ---------------------------------------------------------------------------
# Shared CSS / shell (injected as a Python string, not Jinja extends)
# ---------------------------------------------------------------------------

SHELL_OPEN = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Visual Regression Viewer</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0f0f13; color: #e0e0e8; min-height: 100vh; display: flex; }

#sidebar { width: 270px; min-width: 270px; background: #18181f;
           border-right: 1px solid #252530; display: flex; flex-direction: column;
           position: sticky; top: 0; height: 100vh; overflow-y: auto; }
#sidebar h2 { padding: 14px 16px; font-size: 11px; text-transform: uppercase;
              letter-spacing: .08em; color: #555; border-bottom: 1px solid #252530; }
.results-path { padding: 8px 14px; font-size: 10px; color: #444;
                word-break: break-all; border-bottom: 1px solid #252530; }
.job-item { display: flex; align-items: center; gap: 9px; padding: 9px 14px;
            text-decoration: none; color: #aaa; font-size: 12px;
            border-bottom: 1px solid #1a1a24; transition: background .12s; }
.job-item:hover  { background: #1e1e2a; color: #ddd; }
.job-item.active { background: #1c2038; color: #fff; }
.job-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

#main { flex: 1; padding: 28px 36px; overflow-y: auto; }
h1  { font-size: 20px; margin-bottom: 4px; }
h2s { font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
      color: #666; margin-bottom: 10px; display: block; }

.meta { font-size: 12px; color: #666; margin-bottom: 22px; display: flex;
        gap: 16px; flex-wrap: wrap; align-items: center; }

.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; display: inline-block; }
.dot.pass    { background: #22c55e; }
.dot.fail    { background: #ef4444; }
.dot.unknown { background: #555; }

.badge { display: inline-flex; align-items: center; gap: 5px; padding: 2px 9px;
         border-radius: 4px; font-size: 12px; font-weight: 600; }
.badge.pass    { background: #14301e; color: #22c55e; }
.badge.fail    { background: #2d1212; color: #ef4444; }
.badge.unknown { background: #252530; color: #777; }
.badge.warn    { background: #2d2010; color: #f59e0b; }

/* Images */
.images { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 28px; }
.img-card { background: #141418; border: 1px solid #252530; border-radius: 7px; overflow: hidden; }
.img-card header { padding: 9px 14px; font-size: 11px; text-transform: uppercase;
                   letter-spacing: .06em; color: #666; border-bottom: 1px solid #252530; }
.img-card img { width: 100%; display: block; cursor: zoom-in; }
.no-img { padding: 32px; text-align: center; color: #444; font-size: 12px; }

/* Checks */
.checks { margin-bottom: 28px; }
.check-card { background: #141418; border: 1px solid #252530; border-radius: 7px;
              margin-bottom: 8px; overflow: hidden; }
.check-head { display: flex; align-items: center; gap: 10px; padding: 11px 14px;
              cursor: pointer; user-select: none; }
.check-head:hover { background: #1a1a22; }
.check-title { font-size: 13px; font-weight: 600; flex: 1; }
.check-body { padding: 12px 16px; border-top: 1px solid #1e1e28; font-size: 12px;
              line-height: 1.7; }
.kv { display: flex; gap: 10px; margin-bottom: 3px; }
.kv .k { color: #666; min-width: 190px; flex-shrink: 0; }
.kv .v { color: #c8c8d8; word-break: break-all; }
.errors-list { margin-top: 10px; display: flex; flex-direction: column; gap: 5px; }
.err-item { background: #1a0f0f; border-left: 3px solid #ef4444; padding: 7px 11px;
            border-radius: 0 4px 4px 0; font-size: 11px; font-family: monospace;
            white-space: pre-wrap; word-break: break-all; max-height: 100px;
            overflow-y: auto; color: #d08080; }
.err-item.noise { background: #1a1508; border-left-color: #a16207; color: #c9a038; }
.noise-cat { display: inline-block; font-size: 9px; font-family: monospace;
             background: #2a1f00; color: #a16207; border-radius: 3px;
             padding: 1px 5px; margin-bottom: 4px; text-transform: uppercase;
             letter-spacing: .05em; }

/* Lightbox */
#lb { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.9);
      z-index: 999; align-items: center; justify-content: center; cursor: zoom-out; }
#lb.open { display: flex; }
#lb img { max-width: 96vw; max-height: 96vh; border-radius: 6px; }

/* Home grid */
.stats { display: flex; gap: 28px; margin-bottom: 24px; }
.stat .n { font-size: 30px; font-weight: 700; }
.stat .l { font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: .06em; }
.n.pass { color: #22c55e; } .n.fail { color: #ef4444; } .n.unk { color: #666; }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px,1fr)); gap: 12px; }
.card { background: #141418; border: 1px solid #252530; border-radius: 7px;
        padding: 14px; text-decoration: none; color: inherit;
        transition: border-color .12s, background .12s; display: block; }
.card:hover { background: #1a1a22; border-color: #353548; }
.card h3 { font-size: 13px; margin-bottom: 5px; display: flex; align-items: center; gap: 7px; }
.card .sub { font-size: 11px; color: #555; margin-bottom: 8px; }
.pills { display: flex; gap: 6px; flex-wrap: wrap; }
.pill { font-size: 10px; padding: 2px 6px; border-radius: 3px; }
.pill.ok  { background: #122010; color: #4ade80; }
.pill.err { background: #20100f; color: #f87171; }
</style>
</head>
<body>
<div id="sidebar">
  <h2>Jobs</h2>
  <div class="results-path">{{ results_dir }}</div>
  {% for job in jobs %}
  <a class="job-item{% if active == job.label %} active{% endif %}" href="/job/{{ job.label }}">
    <span class="dot {{ status(job.visual) }}"></span>
    <span class="job-name">{{ job.label }}</span>
  </a>
  {% endfor %}
</div>
<div id="main">
"""

SHELL_CLOSE = """
</div>

<div id="lb" onclick="this.classList.remove('open')">
  <img id="lb-img" src="">
</div>
<script>
document.querySelectorAll('.img-card img').forEach(img => {
  img.onclick = e => {
    document.getElementById('lb-img').src = img.src;
    document.getElementById('lb').classList.add('open');
    e.stopPropagation();
  };
});
document.querySelectorAll('.check-head').forEach(h => {
  const body = h.nextElementSibling;
  h.onclick = () => body.style.display = body.style.display === 'none' ? '' : 'none';
});
</script>
</body></html>
"""

HOME_BODY = """
<h1>Visual Regression Results</h1>
<div class="stats">
  <div class="stat"><div class="n pass">{{ pass_n }}</div><div class="l">Pass</div></div>
  <div class="stat"><div class="n fail">{{ fail_n }}</div><div class="l">Fail</div></div>
  <div class="stat"><div class="n unk">{{ unk_n }}</div><div class="l">No data</div></div>
</div>
<div class="grid">
{% for job in jobs %}
{% set st = status(job.visual) %}
<a class="card" href="/job/{{ job.label }}">
  <h3><span class="dot {{ st }}"></span>{{ job.label }}</h3>
  {% if job.visual %}
  <div class="sub">{{ job.visual.get('repo_id','') }} — {{ job.visual.get('framework','') }}</div>
  <div class="pills">
    {% for name, chk in job.visual.get('checks',{}).items() %}
    <span class="pill {% if chk.get('regression') %}err{% else %}ok{% endif %}">
      {{ name.replace('_',' ') }}
    </span>
    {% endfor %}
  </div>
  {% else %}
  <div class="sub">No visual.json</div>
  {% endif %}
</a>
{% endfor %}
</div>
"""

JOB_BODY = """
<h1>{{ job.label }}</h1>
<div class="meta">
  {% if v %}
  <span>{{ v.get('repo_id','') }}</span>
  <span>{{ v.get('framework','') }}</span>
  <span style="color:#444">{{ v.get('url','') }}</span>
  <span class="badge {{ st }}">
    {% if st == 'pass' %}✓ No regression
    {% elif st == 'fail' %}✗ Regression detected
    {% else %}Unknown{% endif %}
  </span>
  {% if v.get('is_valid') == false %}
  <span class="badge warn">⚠ is_valid = false</span>
  {% endif %}
  {% endif %}
</div>

<div class="images">
  <div class="img-card">
    <header>Baseline</header>
    {% if baseline_img %}<img src="{{ baseline_img }}" alt="Baseline">
    {% else %}<div class="no-img">No baseline image</div>{% endif %}
  </div>
  <div class="img-card">
    <header>Patched</header>
    {% if patched_img %}<img src="{{ patched_img }}" alt="Patched">
    {% else %}<div class="no-img">No patched screenshot</div>{% endif %}
  </div>
</div>

{% if v and v.get('checks') %}
{% set c = v['checks'] %}
<h2s>Signal checks</h2s>
<div class="checks">

  {% if c.get('jaccard_text') is not none %}{% set jt = c['jaccard_text'] %}
  <div class="check-card">
    <div class="check-head">
      <span class="dot {% if jt.get('regression') %}fail{% else %}pass{% endif %}"></span>
      <span class="check-title">Jaccard text similarity</span>
      <span class="badge {% if jt.get('regression') %}fail{% else %}pass{% endif %}">
        {% if jt.get('regression') %}Regression{% else %}OK{% endif %}
      </span>
    </div>
    <div class="check-body">
      <div class="kv"><span class="k">Similarity</span><span class="v">{{ "%.4f"|format(jt.get('similarity',0)) }}</span></div>
      <div class="kv"><span class="k">Threshold</span><span class="v">{{ jt.get('threshold','—') }}</span></div>
    </div>
  </div>
  {% endif %}

  {% if c.get('gpt_visual') is not none %}{% set gv = c['gpt_visual'] %}
  <div class="check-card">
    <div class="check-head">
      <span class="dot {% if gv.get('regression') %}fail{% else %}pass{% endif %}"></span>
      <span class="check-title">GPT visual judgment</span>
      <span class="badge {% if gv.get('regression') %}fail{% else %}pass{% endif %}">
        {% if gv.get('regression') %}Regression{% else %}OK{% endif %}
      </span>
    </div>
    <div class="check-body">
      <div class="kv"><span class="k">Raw response</span><span class="v">{{ gv.get('raw_response','—') }}</span></div>
      {% if gv.get('error') %}<div class="kv"><span class="k">Error</span><span class="v" style="color:#ef4444">{{ gv['error'] }}</span></div>{% endif %}
    </div>
  </div>
  {% endif %}

  {% if c.get('console_errors') is not none %}{% set ce = c['console_errors'] %}
  <div class="check-card">
    <div class="check-head">
      <span class="dot {% if ce.get('regression') %}fail{% else %}pass{% endif %}"></span>
      <span class="check-title">Console errors</span>
      <span class="badge {% if ce.get('regression') %}fail{% else %}pass{% endif %}">
        {% if ce.get('regression') %}{{ ce.get('new_errors',[])|length }} new error(s){% else %}OK{% endif %}
      </span>
      {% if ce.get('filtered_noise') %}
      <span class="badge warn" title="Noise filtered before regression check">{{ ce['filtered_noise']|length }} noise filtered</span>
      {% endif %}
    </div>
    <div class="check-body">
      <div class="kv"><span class="k">Baseline error count</span><span class="v">{{ ce.get('baseline_count','—') }}</span></div>
      <div class="kv"><span class="k">Patched error count</span><span class="v">{{ ce.get('patched_count','—') }}</span></div>
      <div class="kv"><span class="k">Net change</span><span class="v" style="color:{% if ce.get('patched_count',0) > ce.get('baseline_count',0) %}#ef4444{% elif ce.get('patched_count',0) < ce.get('baseline_count',0) %}#22c55e{% else %}#888{% endif %}">{{ '%+d' % (ce.get('patched_count',0) - ce.get('baseline_count',0)) }}</span></div>
      <div class="kv"><span class="k">Signal new errors (after filter)</span><span class="v">{{ ce.get('new_errors',[])|length }}</span></div>
      {% if ce.get('new_errors') %}
      <div style="margin-top:10px;font-size:11px;color:#ef4444;margin-bottom:4px;">▲ {{ ce['new_errors']|length }} introduced (signal)</div>
      <div class="errors-list">
        {% for err in ce['new_errors'] %}
        <div class="err-item">{{ err }}</div>
        {% endfor %}
      </div>
      {% endif %}
      {% if ce.get('fixed_errors') %}
      <div style="margin-top:10px;font-size:11px;color:#22c55e;margin-bottom:4px;">▼ {{ ce['fixed_errors']|length }} fixed</div>
      <div class="errors-list">
        {% for err in ce['fixed_errors'] %}
        <div class="err-item" style="border-left-color:#22c55e;background:#0f1f0f;color:#86efac;">{{ err }}</div>
        {% endfor %}
      </div>
      {% endif %}
      {% if ce.get('filtered_noise') %}
      <div style="margin-top:10px;font-size:11px;color:#a16207;margin-bottom:4px;">⊘ {{ ce['filtered_noise']|length }} noise (localhost artifact, not a regression)</div>
      <div class="errors-list">
        {% for item in ce['filtered_noise'] %}
        <div class="err-item noise"><span class="noise-cat">{{ item.get('category','?') }}</span><br>{{ item.get('error','') }}</div>
        {% endfor %}
      </div>
      {% endif %}
    </div>
  </div>
  {% endif %}

</div>
{% endif %}
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _render(body: str, **ctx):
    template = SHELL_OPEN + body + SHELL_CLOSE
    return render_template_string(template, status=job_status, **ctx)


@app.route("/")
def index():
    jobs = load_jobs()
    return _render(
        HOME_BODY,
        jobs=jobs, active=None,
        results_dir=str(RESULTS_DIR),
        pass_n=sum(1 for j in jobs if job_status(j["visual"]) == "pass"),
        fail_n=sum(1 for j in jobs if job_status(j["visual"]) == "fail"),
        unk_n =sum(1 for j in jobs if job_status(j["visual"]) == "unknown"),
    )


@app.route("/job/<label>")
def job_view(label: str):
    jobs = load_jobs()
    job = next((j for j in jobs if j["label"] == label), None)
    if job is None:
        abort(404)
    visual = job["visual"]
    job_dir = job["dir"]
    return _render(
        JOB_BODY,
        jobs=jobs, active=label,
        results_dir=str(RESULTS_DIR),
        job=job, v=visual,
        st=job_status(visual),
        baseline_img=first_img_url(
            job_dir,
            "visual_work/baseline.png",
            "visual_v2_work/baseline.png",
        ),
        patched_img =img_url(job_dir, "screenshot.png"),
    )


@app.route("/image/<label>/<path:filename>")
def serve_image(label: str, filename: str):
    path = RESULTS_DIR / label / filename
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Visual regression viewer")
    parser.add_argument("--results-dir", required=True,
                        help="Path to a results/ directory (e.g. harness/out/<run>/results)")
    parser.add_argument("--port", type=int, default=7878)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    RESULTS_DIR = Path(args.results_dir).resolve()
    if not RESULTS_DIR.is_dir():
        raise SystemExit(f"Results dir not found: {RESULTS_DIR}")

    print(f"Viewing: {RESULTS_DIR}")
    print(f"Open:    http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
