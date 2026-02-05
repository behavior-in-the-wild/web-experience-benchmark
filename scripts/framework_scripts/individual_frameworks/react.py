import json
from pathlib import Path

# ------------------------
# React detection constants
# ------------------------

REACT_TOOLCHAIN_DEPS = [
    "react-scripts",              # CRA
    "@vitejs/plugin-react",       # Vite React
    "@vitejs/plugin-react-swc",   # Vite React (SWC)
]

REACT_TOOLCHAIN_SCRIPT_TOKENS = [
    "react-scripts",
    "vite",
    "webpack",
    "parcel",
    "esbuild",
]

# ------------------------
# React detection (React SPA/toolchain, excluding Next.js)
# ------------------------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def detect_react(root: Path):
    try:
        # Search root + immediate subdirs for package.json (monorepo-friendly, still cheap)
        pkg_candidates = [root / "package.json"] + list(root.glob("*/package.json"))

        for pkg_path in pkg_candidates:
            if not pkg_path.exists():
                continue

            pkg = _read_json(pkg_path)
            if not pkg:
                continue

            deps = pkg.get("dependencies", {}) or {}
            dev = pkg.get("devDependencies", {}) or {}

            # Exclude Next.js (prevents React bucket swallowing Next repos)
            if ("next" in deps) or ("next" in dev):
                continue

            # Must look like a React app, not just a random library
            has_react = ("react" in deps) or ("react" in dev)
            has_react_dom = ("react-dom" in deps) or ("react-dom" in dev)
            if not (has_react and has_react_dom):
                continue

            # Toolchain fingerprint (CRA/Vite/etc.)
            has_toolchain_dep = any((d in deps) or (d in dev) for d in REACT_TOOLCHAIN_DEPS)

            scripts = (pkg.get("scripts", {}) or {})
            scripts_text = " ".join(str(v).lower() for v in scripts.values())
            has_toolchain_script = any(tok in scripts_text for tok in REACT_TOOLCHAIN_SCRIPT_TOKENS)

            rel_pkg = pkg_path.relative_to(root).as_posix()

            if has_toolchain_dep:
                return True, f"{rel_pkg} declares react + react-dom and React SPA toolchain deps present"

            if has_toolchain_script:
                return True, f"{rel_pkg} declares react + react-dom and scripts indicate SPA bundler/toolchain"

            # Still accept: react + react-dom (your “react in dependencies” pattern)
            return True, f"{rel_pkg} declares dependencies 'react' and 'react-dom'"

    except Exception:
        pass

    return False, None
