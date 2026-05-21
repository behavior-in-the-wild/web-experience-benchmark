#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from openai import OpenAI
from tqdm import tqdm


DANGEROUS = [
    r"\bsudo\b",
    r"\brm\s+-rf\b",
    r"\bcurl\b.*\|\s*(bash|sh)",
    r"\bwget\b.*\|\s*(bash|sh)",
    r"\bssh\b",
    r"\bscp\b",
    r"\bgit\s+push\b",
    r"\bnpm\s+publish\b",
    r"\bfirebase\s+deploy\b",
    r"\bvercel\s+--prod\b",
    r"\bnetlify\s+deploy\s+--prod\b",
    r"\baws\b",
    r"\bgcloud\b",
    r"\baz\b",
]

ALLOWED_FIRST = {
    "npm", "npx", "pnpm", "yarn", "bun",
    "node", "python", "python3", "pip", "pip3",
    "bundle", "hugo", "jekyll", "mkdocs",
    "gatsby", "vite", "next", "astro", "make"
}

ERROR_TEXT = [
    "cannot get /",
    "404 not found",
    "500 internal server error",
    "application error",
    "module not found",
    "build failed",
]


def normalize_repo(line: str) -> Optional[Tuple[str, str, str]]:
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    s = s.replace("https://github.com/", "")
    s = s.replace("http://github.com/", "")
    s = s.replace("git@github.com:", "")
    if s.endswith(".git"):
        s = s[:-4]
    s = s.strip("/")

    parts = s.split("/")
    if len(parts) < 2:
        return None

    owner, repo = parts[0].strip(), parts[1].strip()
    if not owner or not repo:
        return None

    repo_id = f"{owner}/{repo}"
    repo_url = f"https://github.com/{repo_id}"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{owner}__{repo}")
    return repo_id, repo_url, safe


def run_cmd(cmd: List[str], cwd: Optional[Path], log_path: Path, timeout: int) -> Tuple[bool, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        try:
            p = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                text=True,
            )
            return p.returncode == 0, "" if p.returncode == 0 else f"exit code {p.returncode}"
        except subprocess.TimeoutExpired:
            return False, f"timeout after {timeout}s"
        except Exception as e:
            return False, str(e)


def read_text(path: Path, max_chars: int = 30000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def find_readme(repo_dir: Path) -> Optional[Path]:
    for name in ["README.md", "readme.md", "Readme.md", "README", "README.mdx", "readme.mdx"]:
        p = repo_dir / name
        if p.exists() and p.is_file():
            return p
    return None


def load_package_json(project_dir: Path) -> Dict:
    p = project_dir / "package.json"
    if not p.exists():
        return {}
    try:
        return json.loads(read_text(p, 200000))
    except Exception:
        return {}


def deps_from_package(pkg: Dict) -> Dict:
    out = {}
    for k in ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]:
        if isinstance(pkg.get(k), dict):
            out.update(pkg[k])
    return out


def project_roots(repo_dir: Path) -> List[Path]:
    roots = [repo_dir]
    markers = {
        "package.json", "index.html", "mkdocs.yml", "_config.yml",
        "hugo.toml", "hugo.yaml", "hugo.yml", "config.toml",
        "vite.config.js", "vite.config.ts",
        "next.config.js", "next.config.mjs",
        "astro.config.mjs", "gatsby-config.js"
    }

    for child in repo_dir.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in {"node_modules", "dist", "build", ".next", "public", "_site", "site"}:
            continue
        try:
            names = {x.name for x in child.iterdir()}
            if names & markers:
                roots.append(child)
        except Exception:
            pass

    return roots[:10]


def summarize_tree(repo_dir: Path, max_files: int = 250) -> List[str]:
    ignore = {".git", "node_modules", ".next", "dist", "build", "_site", "site", ".cache"}
    files = []
    for p in repo_dir.rglob("*"):
        rel = p.relative_to(repo_dir)
        if any(part in ignore for part in rel.parts):
            continue
        if p.is_file():
            files.append(str(rel))
        if len(files) >= max_files:
            break
    return files


def detect_root(repo_dir: Path, root: Path) -> Dict:
    pkg = load_package_json(root)
    deps = deps_from_package(pkg)
    scripts = pkg.get("scripts", {}) if isinstance(pkg.get("scripts"), dict) else {}

    rel = "." if root == repo_dir else str(root.relative_to(repo_dir))

    def has(pattern: str) -> bool:
        return bool(list(root.glob(pattern)))

    framework = "unknown"
    confidence = "low"
    evidence = []

    if "next" in deps or has("next.config.*"):
        framework, confidence = "next", "high"
        evidence.append("Next.js detected from next dependency or next.config.*")
    elif "astro" in deps or has("astro.config.*"):
        framework, confidence = "astro", "high"
        evidence.append("Astro detected from astro dependency or astro.config.*")
    elif "gatsby" in deps or has("gatsby-config.*"):
        framework, confidence = "gatsby", "high"
        evidence.append("Gatsby detected from gatsby dependency or gatsby-config.*")
    elif "vite" in deps or has("vite.config.*"):
        framework, confidence = "vite", "high"
        evidence.append("Vite detected from vite dependency or vite.config.*")
    elif "react" in deps and "react-dom" in deps:
        framework, confidence = "react", "medium"
        evidence.append("React detected from react/react-dom dependencies")
    elif "vue" in deps:
        framework, confidence = "vue", "medium"
        evidence.append("Vue detected from vue dependency")
    elif has("hugo.toml") or has("hugo.yaml") or has("hugo.yml") or ((root / "content").exists() and (root / "layouts").exists()):
        framework, confidence = "hugo", "high"
        evidence.append("Hugo detected from config/content/layouts")
    elif has("_config.yml") and ((root / "Gemfile").exists() or (root / "_posts").exists()):
        framework, confidence = "jekyll", "high"
        evidence.append("Jekyll detected from _config.yml with Gemfile/_posts")
    elif has("mkdocs.yml"):
        framework, confidence = "mkdocs", "high"
        evidence.append("MkDocs detected from mkdocs.yml")
    elif "express" in deps:
        framework, confidence = "express", "medium"
        evidence.append("Express detected from express dependency")
    elif (root / "index.html").exists():
        framework, confidence = "static-html", "medium"
        evidence.append("Static HTML detected from index.html")

    pm = "npm"
    if (root / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (root / "yarn.lock").exists():
        pm = "yarn"
    elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        pm = "bun"

    score = {"high": 3, "medium": 2, "low": 1}.get(confidence, 0)
    if framework != "unknown":
        score += 3
    if "build" in scripts:
        score += 2
    if rel == ".":
        score += 1

    return {
        "project_root": rel,
        "framework": framework,
        "confidence": confidence,
        "package_manager": pm,
        "scripts": scripts,
        "deps": sorted(list(deps.keys()))[:100],
        "evidence": evidence,
        "score": score,
    }


def detect(repo_dir: Path) -> Dict:
    detections = [detect_root(repo_dir, r) for r in project_roots(repo_dir)]
    detections.sort(key=lambda x: x["score"], reverse=True)
    best = detections[0] if detections else {
        "project_root": ".",
        "framework": "unknown",
        "confidence": "low",
        "package_manager": "npm",
        "scripts": {},
        "deps": [],
        "evidence": [],
        "score": 0,
    }
    best["files"] = summarize_tree(repo_dir)
    best["candidate_roots"] = [
        {
            "project_root": d.get("project_root"),
            "framework": d.get("framework"),
            "confidence": d.get("confidence"),
            "package_manager": d.get("package_manager"),
            "score": d.get("score"),
            "evidence": d.get("evidence", []),
        }
        for d in detections[:6]
    ]
    return best


def extract_readme_evidence(readme: str) -> str:
    if not readme:
        return ""
    terms = [
        "deploy", "deployment", "hosting", "production", "build", "install",
        "getting started", "quick start", "vercel", "netlify", "firebase",
        "cloudflare", "github pages", "serve", "local development"
    ]
    lines = readme.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    hits = [i for i, line in enumerate(lines) if any(t in line.lower() for t in terms)]

    if not hits:
        return readme[:12000]

    ranges = []
    for i in hits[:35]:
        a, b = max(0, i - 5), min(len(lines), i + 20)
        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], b))
        else:
            ranges.append((a, b))

    chunks = ["\n".join(lines[a:b]).strip() for a, b in ranges]
    return "\n\n---\n\n".join(chunks)[:14000]


def collect_configs(repo_dir: Path) -> str:
    names = [
        "package.json", "vercel.json", "netlify.toml", "firebase.json",
        "wrangler.toml", "Dockerfile", "docker-compose.yml", "mkdocs.yml",
        "_config.yml", "hugo.toml", "hugo.yaml", "hugo.yml", "config.toml",
        "vite.config.js", "vite.config.ts", "next.config.js", "next.config.mjs",
        "astro.config.mjs", "gatsby-config.js", "Makefile"
    ]
    chunks = []
    for root in project_roots(repo_dir):
        rel = "." if root == repo_dir else str(root.relative_to(repo_dir))
        for name in names:
            p = root / name
            if p.exists() and p.is_file():
                label = name if rel == "." else f"{rel}/{name}"
                chunks.append(f"\n--- {label} ---\n{read_text(p, 5000)}")

    wf = repo_dir / ".github" / "workflows"
    if wf.exists():
        for p in list(wf.glob("*.yml"))[:4] + list(wf.glob("*.yaml"))[:4]:
            chunks.append(f"\n--- {p.relative_to(repo_dir)} ---\n{read_text(p, 4000)}")

    return "\n".join(chunks)[:18000]


def pm_install(pm: str) -> str:
    return {"pnpm": "pnpm install", "yarn": "yarn install", "bun": "bun install"}.get(pm, "npm install")


def pm_run(pm: str, script: str) -> str:
    if pm == "pnpm":
        return f"pnpm run {script}"
    if pm == "yarn":
        return f"yarn {script}"
    if pm == "bun":
        return f"bun run {script}"
    return f"npm run {script}"


def deterministic_plan(d: Dict) -> Dict:
    fw = d["framework"]
    pm = d["package_manager"]
    scripts = d.get("scripts", {})
    wd = d.get("project_root", ".")

    install, build, serve = [], [], []
    output = ""

    if fw == "static-html":
        output = "."
        serve = ['python3 -m http.server "$PORT"']
    elif fw in {"vite", "react", "vue", "astro"}:
        install = [pm_install(pm)]
        build = [pm_run(pm, "build")] if "build" in scripts else ["npx vite build"]
        output = "dist"
        serve = ['python3 -m http.server "$PORT" -d dist']
    elif fw == "gatsby":
        install = [pm_install(pm)]
        build = [pm_run(pm, "build")] if "build" in scripts else ["npx gatsby build"]
        output = "public"
        serve = ['python3 -m http.server "$PORT" -d public']
    elif fw == "next":
        install = [pm_install(pm)]
        build = [pm_run(pm, "build")] if "build" in scripts else ["npx next build"]
        output = ".next"
        serve = [pm_run(pm, "start") + ' -- -p "$PORT"'] if "start" in scripts else ['npx next start -p "$PORT"']
    elif fw == "hugo":
        build = ["hugo --minify"]
        output = "public"
        serve = ['python3 -m http.server "$PORT" -d public']
    elif fw == "jekyll":
        install = ["bundle install"]
        build = ["bundle exec jekyll build"]
        output = "_site"
        serve = ['python3 -m http.server "$PORT" -d _site']
    elif fw == "mkdocs":
        install = ["python3 -m pip install mkdocs"]
        build = ["mkdocs build"]
        output = "site"
        serve = ['python3 -m http.server "$PORT" -d site']
    elif fw == "express":
        install = [pm_install(pm)]
        output = "."
        serve = ['PORT="$PORT" ' + pm_run(pm, "start")] if "start" in scripts else []
    else:
        return {
            "framework": fw,
            "confidence": "low",
            "package_manager": pm,
            "working_directory": wd,
            "install_commands": [],
            "build_commands": [],
            "serve_commands": [],
            "output_dir": "",
            "evidence": d.get("evidence", []),
            "warnings": ["No deterministic deployment path found"],
            "should_try": False,
        }

    return {
        "framework": fw,
        "confidence": d.get("confidence", "medium"),
        "package_manager": pm,
        "working_directory": wd,
        "install_commands": install,
        "build_commands": build,
        "serve_commands": serve,
        "output_dir": output,
        "evidence": d.get("evidence", []),
        "warnings": [],
        "should_try": True,
    }


def parse_json(text: str) -> Optional[Dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            return None
    return None


def azure_plan(repo_id: str, detection: Dict, readme_evidence: str, configs: str) -> Optional[Dict]:
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    api_key = os.environ["AZURE_OPENAI_API_KEY"]
    deployment = os.environ["AZURE_OPENAI_API_DEPLOYMENT_NAME"]

    base_url = endpoint + "/openai/v1/" if "/openai/v1" not in endpoint else endpoint.rstrip("/") + "/"
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=240)

    prompt = f"""
Return JSON only. Do not return markdown.

You are generating a SAFE LOCAL deployment plan for a public website repository.

Goal:
Serve the website locally at http://127.0.0.1:$PORT.

Evidence priority:
1. README deployment/build/local-development instructions are the highest-priority human-authored evidence.
2. package.json scripts and lockfiles are second priority.
3. config files such as vite.config, next.config, astro.config, gatsby-config, mkdocs.yml, hugo.toml, _config.yml are third priority.
4. deterministic framework detection is only fallback evidence.

Rules:
- Use README commands when they are safe and consistent with repository files.
- If README gives a working directory or subfolder, use it as working_directory.
- If README says npm install / npm run build / npm start, prefer that over invented commands.
- If README mentions yarn, pnpm, or bun, prefer that package manager if the relevant lockfile or package scripts support it.
- If README gives production deploy commands such as firebase deploy, vercel --prod, netlify deploy --prod, translate them into local build + local serve.
- Do not run production deployment commands.
- Do not use firebase deploy, vercel --prod, netlify deploy --prod, git push, npm publish, ssh, scp, aws, gcloud, az, sudo, rm -rf.
- Do not invent files or scripts.
- If root/docs/public/dist/build/out/site/_site contains index.html, prefer static serving before heavy framework build.
- For GitHub Pages or Jekyll repos, if index.html exists, prefer static serving first instead of bundle/jekyll.
- For React/Vite/Vue/Astro, prefer install -> build -> serve dist or build.
- For Next.js, prefer install -> build -> next start unless static export/output exists.
- For Gatsby, prefer install -> build -> serve public.
- For Hugo, prefer hugo -> serve public.
- For Eleventy, prefer npx @11ty/eleventy -> serve _site. Do not use unsupported --host flags.
- If uncertain, set should_try=false.
- Final serve command must keep the server running.

Required JSON keys:
framework, confidence, package_manager, working_directory, install_commands, build_commands, serve_commands, output_dir, evidence, warnings, should_try

Repository:
{repo_id}

Deterministic detection:
{json.dumps(detection, indent=2)[:15000]}

README evidence:
{readme_evidence[:14000]}

Config/package/workflow evidence:
{configs[:16000]}
""".strip()

    try:
        resp = client.responses.create(
            model=deployment,
            input=prompt,
            max_output_tokens=1600,
        )
        return parse_json(resp.output_text)
    except Exception as e:
        print(f"[WARN] Azure call failed for {repo_id}: {e}")
        return None


def strip_env_prefix(cmd: str) -> str:
    s = cmd.strip()
    while True:
        m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*=(".*?"|\'.*?\'|[^ ]+)\s+', s)
        if not m:
            break
        s = s[m.end():].strip()
    return s


def command_safe(cmd: str) -> Tuple[bool, str]:
    cmd = str(cmd).strip()
    if not cmd:
        return False, "empty command"
    if "\n" in cmd or "\r" in cmd:
        return False, "multiline command rejected"
    if len(cmd) > 350:
        return False, "command too long"

    low = cmd.lower()
    for pat in DANGEROUS:
        if re.search(pat, low):
            return False, f"dangerous command pattern: {pat}"

    try:
        parts = shlex.split(strip_env_prefix(cmd))
    except Exception:
        return False, "cannot parse command"

    if not parts:
        return False, "empty parsed command"

    if parts[0] not in ALLOWED_FIRST:
        return False, f"unsupported command token: {parts[0]}"

    return True, ""


def validate_plan(plan: Dict) -> Tuple[bool, str]:
    if not isinstance(plan, dict):
        return False, "plan is not JSON object"
    if not plan.get("should_try", False):
        return False, "should_try=false"

    wd = str(plan.get("working_directory", ".")).strip() or "."
    if wd.startswith("/") or ".." in Path(wd).parts:
        return False, "unsafe working_directory"
    plan["working_directory"] = wd

    for key in ["install_commands", "build_commands", "serve_commands"]:
        if key not in plan:
            plan[key] = []
        if not isinstance(plan[key], list):
            return False, f"{key} must be list"
        cleaned = []
        for cmd in plan[key]:
            ok, why = command_safe(str(cmd))
            if not ok:
                return False, f"{key}: {why}: {cmd}"
            cleaned.append(str(cmd).strip())
        plan[key] = cleaned

    if not plan["serve_commands"]:
        return False, "no serve command"

    return True, ""


def generate_host(plan: Dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    wd = plan.get("working_directory", ".")
    output = str(plan.get("output_dir", "")).strip()
    serve = plan["serve_commands"][-1]

    static_fallback = bool(output) and ("http.server" in serve or "serve " in serve or "npx serve" in serve)

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'PORT="${PORT:-3000}"',
        'REPO_DIR="${REPO_DIR:-$(pwd)}"',
        'LOG_DIR="${LOG_DIR:-$REPO_DIR/deploy_logs}"',
        'mkdir -p "$LOG_DIR"',
        'cd "$REPO_DIR"',
        f"cd {shlex.quote(wd)}",
        'echo "[INFO] Starting host.sh on port $PORT" | tee -a "$LOG_DIR/host_runtime.log"',
        "run_step() {",
        '  echo "[RUN] $1" | tee -a "$LOG_DIR/host_runtime.log"',
        '  bash -lc "$1" 2>&1 | tee -a "$LOG_DIR/host_runtime.log"',
        "}",
        "",
    ]

    for c in plan.get("install_commands", []):
        lines.append(f"run_step {shlex.quote(c)}")
    for c in plan.get("build_commands", []):
        lines.append(f"run_step {shlex.quote(c)}")

    if static_fallback:
        lines += [
            "",
            f"preferred={shlex.quote(output)}",
            'for d in "$preferred" dist build public out _site site .; do',
            '  if [ "$d" = "." ] || [ -d "$d" ]; then',
            '    echo "[SERVE] python3 -m http.server $PORT -d $d" | tee -a "$LOG_DIR/host_runtime.log"',
            '    exec python3 -m http.server "$PORT" -d "$d"',
            "  fi",
            "done",
            'echo "[ERROR] no output directory found" | tee -a "$LOG_DIR/host_runtime.log"',
            "exit 41",
        ]
    else:
        lines += [
            "",
            f'echo "[SERVE] {serve}" | tee -a "$LOG_DIR/host_runtime.log"',
            f"exec bash -lc {shlex.quote(serve)}",
        ]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def clean_env(repo_dir: Path, log_dir: Path, port: int) -> Dict[str, str]:
    env = {}

    safe_home = repo_dir / ".autodeploy_home"
    safe_home.mkdir(parents=True, exist_ok=True)

    gem_home = safe_home / "gems"
    bundle_path = safe_home / "bundle"
    npm_cache = safe_home / "npm-cache"
    yarn_cache = safe_home / "yarn-cache"
    pnpm_home = safe_home / "pnpm-home"

    for d in [gem_home, bundle_path, npm_cache, yarn_cache, pnpm_home]:
        d.mkdir(parents=True, exist_ok=True)

    original_home = os.environ.get("HOME", "")
    original_path = os.environ.get("PATH", "")

    extra_paths = [
        str(gem_home / "bin"),
        str(pnpm_home),
        str(safe_home / ".bun" / "bin"),
        str(Path(original_home) / ".bun" / "bin") if original_home else "",
        str(Path(original_home) / ".npm-global" / "bin") if original_home else "",
    ]

    env["PATH"] = ":".join([x for x in extra_paths if x] + [original_path])
    env["HOME"] = str(safe_home.resolve())

    # Keep language/shell basics only.
    for k in ["LANG", "LC_ALL", "SHELL", "USER"]:
        if os.environ.get(k):
            env[k] = os.environ[k]

    # Ruby/Bundler local install paths.
    env["GEM_HOME"] = str(gem_home.resolve())
    env["GEM_PATH"] = str(gem_home.resolve())
    env["BUNDLE_PATH"] = str(bundle_path.resolve())
    env["BUNDLE_USER_HOME"] = str((safe_home / "bundle-user-home").resolve())
    env["BUNDLE_APP_CONFIG"] = str((safe_home / "bundle-config").resolve())

    # Node package-manager caches should stay inside repo-local temp home.
    env["NPM_CONFIG_CACHE"] = str(npm_cache.resolve())
    env["npm_config_cache"] = str(npm_cache.resolve())
    env["YARN_CACHE_FOLDER"] = str(yarn_cache.resolve())
    env["PNPM_HOME"] = str(pnpm_home.resolve())

    env["PORT"] = str(port)
    env["REPO_DIR"] = str(repo_dir.resolve())
    env["LOG_DIR"] = str(log_dir.resolve())
    env["CI"] = "true"
    env["NO_COLOR"] = "1"

    return env


def run_and_validate(script: Path, repo_dir: Path, log_dir: Path, port: int, timeout: int) -> Tuple[bool, str, Dict]:
    log_dir.mkdir(parents=True, exist_ok=True)
    run_log = log_dir / "run_host.log"
    env = clean_env(repo_dir, log_dir, port)

    with open(run_log, "w", encoding="utf-8", errors="replace") as f:
        try:
            p = subprocess.Popen(
                ["bash", str(script.resolve())],
                cwd=str(repo_dir),
                stdout=f,
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid,
                text=True,
            )
        except Exception as e:
            return False, f"SERVE_FAILED: {e}", {}

    url = f"http://127.0.0.1:{port}"
    meta = {"url": url, "status_code": None, "body_len": 0, "title": None}

    try:
        last = ""
        for _ in range(timeout):
            if p.poll() is not None:
                return False, f"SERVE_FAILED: exited early code {p.returncode}", meta
            try:
                r = requests.get(url, timeout=4, allow_redirects=True)
                body = r.text or ""
                meta["status_code"] = r.status_code
                meta["body_len"] = len(body)

                m = re.search(r"<title[^>]*>(.*?)</title>", body, flags=re.I | re.S)
                if m:
                    meta["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:200]

                bad = any(x in body.lower()[:8000] for x in ERROR_TEXT)
                if r.status_code < 400 and len(body) >= 50 and not bad:
                    return True, "success", meta

                last = f"HTTP={r.status_code}, body_len={len(body)}, error_text={bad}"
            except Exception as e:
                last = str(e)
            time.sleep(1)

        return False, f"LOCALHOST_NOT_VALID: {last}", meta

    finally:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            time.sleep(1)
            if p.poll() is None:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass


def append_jsonl(path: Path, obj: Dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def load_processed(path: Path) -> set:
    done = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
            if obj.get("repo_id"):
                done.add(obj["repo_id"])
        except Exception:
            pass
    return done


def process_one(idx: int, line: str, args, processed: set) -> Dict:
    norm = normalize_repo(line)
    if not norm:
        return {"repo_id": line, "status": "failed", "failure_reason": "INVALID_REPO_LINE"}

    repo_id, repo_url, safe = norm
    if repo_id in processed and not args.force:
        return {"repo_id": repo_id, "repo_url": repo_url, "status": "skipped", "failure_reason": "already checkpointed"}

    clone_dir = Path(args.clone_dir) / safe
    out_dir = Path(args.output) / safe
    log_dir = Path(args.logs) / safe
    script_path = out_dir / "host.sh"
    metadata_path = out_dir / "deploy_metadata.json"
    port = args.port + idx

    record = {
        "repo_id": repo_id,
        "repo_url": repo_url,
        "safe_name": safe,
        "port": port,
        "script_path": str(script_path),
        "status": "failed",
        "failure_reason": None,
        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    try:
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        if not clone_dir.exists() or args.reclone:
            if clone_dir.exists():
                subprocess.run(["rm", "-rf", str(clone_dir)], check=False)
            ok, why = run_cmd(["git", "clone", "--depth=1", repo_url, str(clone_dir)], None, log_dir / "clone.log", args.timeout_clone)
            if not ok:
                record["failure_reason"] = f"CLONE_FAILED: {why}"
                append_jsonl(Path(args.checkpoint), record)
                return record

        d = detect(clone_dir)
        readme_path = find_readme(clone_dir)
        readme = read_text(readme_path, 60000) if readme_path else ""
        readme_evidence = extract_readme_evidence(readme)
        configs = collect_configs(clone_dir)

        plan = deterministic_plan(d)
        plan_source = "deterministic"

        if args.use_azure_openai:
            llm_plan = azure_plan(repo_id, d, readme_evidence, configs)
            if llm_plan:
                ok, why = validate_plan(llm_plan)
                if ok:
                    plan = llm_plan
                    plan_source = f"azure:{os.environ.get('AZURE_OPENAI_API_DEPLOYMENT_NAME', 'unknown')}"
                else:
                    record["llm_rejected_reason"] = why

        ok, why = validate_plan(plan)
        record["detected_framework"] = plan.get("framework")
        record["confidence"] = plan.get("confidence")
        record["plan_source"] = plan_source
        record["working_directory"] = plan.get("working_directory")
        record["output_dir"] = plan.get("output_dir")
        record["install_commands"] = plan.get("install_commands")
        record["build_commands"] = plan.get("build_commands")
        record["serve_commands"] = plan.get("serve_commands")

        if not ok:
            record["failure_reason"] = f"PLAN_INVALID: {why}"
            append_jsonl(Path(args.checkpoint), record)
            return record

        generate_host(plan, script_path)

        metadata = {
            "repo_id": repo_id,
            "repo_url": repo_url,
            "readme_path": str(readme_path.relative_to(clone_dir)) if readme_path else None,
            "detection": d,
            "plan": plan,
            "plan_source": plan_source,
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

        ok, why, meta = run_and_validate(script_path, clone_dir, log_dir, port, args.timeout_serve)
        record["validation"] = meta

        if ok:
            record["status"] = "success"
        else:
            record["failure_reason"] = why

        append_jsonl(Path(args.checkpoint), record)
        return record

    except Exception as e:
        record["failure_reason"] = f"UNKNOWN_ERROR: {e}"
        append_jsonl(Path(args.checkpoint), record)
        return record


def write_reports(checkpoint: Path, report_dir: Path):
    report_dir.mkdir(parents=True, exist_ok=True)
    latest = {}

    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
                if obj.get("repo_id"):
                    latest[obj["repo_id"]] = obj
            except Exception:
                pass

    records = list(latest.values())
    success = [r for r in records if r.get("status") == "success"]
    failed = [r for r in records if r.get("status") == "failed"]

    (report_dir / "deployment_success.txt").write_text("\n".join(r["repo_url"] for r in success if r.get("repo_url")) + ("\n" if success else ""), encoding="utf-8")
    (report_dir / "deployment_failed.txt").write_text("\n".join(f'{r.get("repo_url", r.get("repo_id"))}\t{r.get("failure_reason")}' for r in failed) + ("\n" if failed else ""), encoding="utf-8")

    fields = ["repo_id", "repo_url", "status", "failure_reason", "detected_framework", "confidence", "plan_source", "working_directory", "output_dir", "port", "script_path"]
    with open(report_dir / "deployment_summary.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k) for k in fields})

    counts = {}
    for r in failed:
        code = str(r.get("failure_reason") or "UNKNOWN").split(":", 1)[0]
        counts[code] = counts.get(code, 0) + 1
    (report_dir / "failure_counts.txt").write_text("\n".join(f"{v}\t{k}" for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)) + ("\n" if counts else ""), encoding="utf-8")

    return len(records), len(success), len(failed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", default="generated_scripts_azure")
    ap.add_argument("--clone-dir", default="cloned_repos_azure")
    ap.add_argument("--logs", default="logs_azure")
    ap.add_argument("--report-dir", default="reports_azure")
    ap.add_argument("--checkpoint", default="deployment_checkpoint_azure.jsonl")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--port", type=int, default=4000)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--timeout-clone", type=int, default=300)
    ap.add_argument("--timeout-serve", type=int, default=600)
    ap.add_argument("--use-azure-openai", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reclone", action="store_true")
    args = ap.parse_args()

    if args.use_azure_openai:
        for k in ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_DEPLOYMENT_NAME"]:
            if not os.environ.get(k):
                raise SystemExit(f"Missing env var: {k}")

    lines = Path(args.input).read_text(encoding="utf-8", errors="replace").splitlines()
    lines = [x for x in lines if x.strip() and not x.strip().startswith("#")]
    if args.limit:
        lines = lines[:args.limit]

    processed = load_processed(Path(args.checkpoint))

    print(f"[INFO] repos={len(lines)} already_checkpointed={len(processed)} workers={args.workers} azure={args.use_azure_openai}")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(process_one, i, line, args, processed) for i, line in enumerate(lines)]
        for fut in tqdm(as_completed(futures), total=len(futures), desc="repos"):
            results.append(fut.result())

    total, succ, fail = write_reports(Path(args.checkpoint), Path(args.report_dir))
    cur_succ = sum(1 for r in results if r.get("status") == "success")
    cur_fail = sum(1 for r in results if r.get("status") == "failed")
    cur_skip = sum(1 for r in results if r.get("status") == "skipped")

    print(f"[DONE] current_run success={cur_succ} failed={cur_fail} skipped={cur_skip}")
    print(f"[DONE] checkpoint_total={total} success={succ} failed={fail}")
    print(f"[DONE] reports={args.report_dir}")


if __name__ == "__main__":
    main()
