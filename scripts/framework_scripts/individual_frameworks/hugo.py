from pathlib import Path

# ------------------------
# Hugo detection constants
# ------------------------

HUGO_CONFIG_FILES_STRONG = ["hugo.toml", "hugo.yaml", "hugo.yml"]
HUGO_CONFIG_FILES_GENERIC = ["config.toml", "config.yaml", "config.yml"]

HUGO_PROJECT_DIRS = [
    "content", "layouts", "themes", "archetypes",
    "static", "assets", "data", "i18n",
    "config/_default",
]

HUGO_HTML_GENERATOR_NEEDLE = 'name="generator"'
HUGO_NEEDLE = "hugo"

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def detect_hugo(root: Path):
    try:
        # 1) Build-output signature: Hugo generator in HTML / feeds
        # Check a small, targeted set to stay fast
        candidates = [
            root / "index.html",
            root / "index.xml",
        ]
        # also scan a few common output index.html locations (taxonomies, pages)
        candidates += list(root.glob("**/index.html"))[:20]
        candidates += list(root.glob("**/index.xml"))[:20]

        for p in candidates:
            if not p.exists() or not p.is_file():
                continue
            txt = _read_text(p).lower()
            if p.name.endswith(".html"):
                # meta generator
                if HUGO_HTML_GENERATOR_NEEDLE in txt and HUGO_NEEDLE in txt:
                    return True, f"{p.relative_to(root).as_posix()} contains Hugo generator meta tag"
                # common footer text (supporting)
                if "powered by hugo" in txt:
                    return True, f"{p.relative_to(root).as_posix()} contains 'Powered by Hugo'"
            else:
                # feeds: <generator>Hugo</generator>
                if "<generator>" in txt and "hugo" in txt:
                    return True, f"{p.relative_to(root).as_posix()} contains Hugo generator tag"

        # 2) Source signature: Hugo config + Hugo structure
        # Strong config names are enough on their own in most repos
        for cfg in HUGO_CONFIG_FILES_STRONG:
            if (root / cfg).exists():
                return True, f"Found Hugo config file: {cfg}"

        # Generic config requires Hugo structure dirs to avoid false positives
        has_generic_cfg = any((root / cfg).exists() for cfg in HUGO_CONFIG_FILES_GENERIC)
        if has_generic_cfg:
            for d in HUGO_PROJECT_DIRS:
                if (root / d).exists():
                    return True, "Found config.(toml|yaml|yml) with Hugo project structure"

    except Exception:
        pass

    return False, None
