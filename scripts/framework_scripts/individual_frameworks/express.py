import json
from pathlib import Path

# ------------------------
# Express detection constants
# ------------------------

EXPRESS_ENTRY_FILES = [
    "server.js", "app.js", "index.js",
    "backend/server.js", "backend/app.js", "backend/index.js",
]

EXPRESS_CODE_MARKERS = [
    "require('express')",
    'require("express")',
    "from 'express'",
    'from "express"',
    "import express",
    "express()",
    "express.router",
    "express.Router",
    "app.listen(",
]

# ------------------------
# Express detection (package.json + optional code confirmation)
# ------------------------

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def detect_express(root: Path):
    try:
        # Scan root + one-level subdirs for package.json (monorepo-friendly)
        pkg_candidates = [root / "package.json"] + list(root.glob("*/package.json"))

        for pkg_path in pkg_candidates:
            if not pkg_path.exists():
                continue

            try:
                pkg = json.loads(_read_text(pkg_path))
            except Exception:
                continue

            deps = pkg.get("dependencies", {}) or {}
            dev = pkg.get("devDependencies", {}) or {}

            if ("express" not in deps) and ("express" not in dev):
                continue

            rel_pkg = pkg_path.relative_to(root).as_posix()

            # High-confidence: confirm code usage in common entry files
            for rel in EXPRESS_ENTRY_FILES:
                fp = root / rel
                if fp.exists():
                    txt = _read_text(fp).lower()
                    if ("express()" in txt) and ("listen(" in txt):
                        return True, f"{rel_pkg} declares 'express' and {rel} contains express() + listen()"
                    if any(m.lower() in txt for m in EXPRESS_CODE_MARKERS):
                        return True, f"{rel_pkg} declares 'express' and {rel} contains Express usage markers"

            # Medium-confidence fallback: dependency alone (matches your dataset pattern)
            return True, f"{rel_pkg} declares dependency 'express'"

    except Exception:
        pass

    return False, None
