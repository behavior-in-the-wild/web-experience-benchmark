import json
from pathlib import Path

# ------------------------
# Next.js detection (package.json OR build artifacts)
# ------------------------

NEXTJS_CONFIG_FILES = ["next.config.js", "next.config.mjs", "next.config.ts"]

NEXTJS_BUILD_ARTIFACTS = [
    Path("_next/static/chunks/_buildManifest.js"),
    Path("_next/static/chunks/_ssgManifest.js"),
    Path(".next/BUILD_ID"),
    Path(".next/routes-manifest.json"),
    Path("out/_next/static/chunks/_buildManifest.js"),
    Path("out/_next/static/chunks/_ssgManifest.js"),
]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _next_build_evidence(root: Path):
    for rel in NEXTJS_BUILD_ARTIFACTS:
        if (root / rel).exists():
            return True, f"Found Next.js build artifact: {rel.as_posix()}"
    return False, None


def _next_source_evidence(root: Path):
    # root + immediate subdirs (monorepo-friendly, avoids relying on "website/" names)
    pkg_candidates = [root / "package.json"] + list(root.glob("*/package.json"))

    for pkg_path in pkg_candidates:
        if not pkg_path.exists():
            continue

        pkg = _read_json(pkg_path)
        if not pkg:
            continue

        deps = pkg.get("dependencies", {}) or {}
        dev = pkg.get("devDependencies", {}) or {}
        if "next" not in deps and "next" not in dev:
            continue

        app_dir = pkg_path.parent
        rel_pkg = pkg_path.relative_to(root).as_posix()

        scripts = (pkg.get("scripts", {}) or {})
        scripts_text = " ".join(str(v).lower() for v in scripts.values())
        if "next dev" in scripts_text or "next build" in scripts_text or "next start" in scripts_text:
            return True, f"{rel_pkg} declares 'next' and scripts invoke Next CLI"

        for cfg in NEXTJS_CONFIG_FILES:
            if (app_dir / cfg).exists():
                return True, f"{rel_pkg} declares 'next' and {cfg} exists"

        return True, f"{rel_pkg} declares dependency 'next'"

    return False, None


def detect_nextjs(root: Path):
    try:
        ok, ev = _next_build_evidence(root)
        if ok:
            return True, ev

        ok, ev = _next_source_evidence(root)
        if ok:
            return True, ev

    except Exception:
        pass

    return False, None
