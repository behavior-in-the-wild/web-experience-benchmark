from pathlib import Path

# ------------------------
# Pelican detection constants
# ------------------------

PELICAN_CONFIG_FILES = ["pelicanconf.py", "publishconf.py"]

PELICAN_TEXT_NEEDLES = [
    "pelican",
    "powered by pelican",
]

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


# ------------------------
# Pelican detection
# ------------------------

def detect_pelican(root: Path):
    try:
        # 1) Python config files (strong signal)
        if all((root / f).exists() for f in PELICAN_CONFIG_FILES):
            return True, "Found pelicanconf.py and publishconf.py"

        # 2) requirements.txt
        req = root / "requirements.txt"
        if req.exists():
            txt = _read_text(req).lower()
            if "pelican" in txt:
                return True, "requirements.txt declares 'pelican'"

        # 3) README evidence
        for name in ["README.md", "README.rst", "README.txt"]:
            p = root / name
            if p.exists():
                txt = _read_text(p).lower()
                if any(n in txt for n in PELICAN_TEXT_NEEDLES):
                    return True, f"{name} mentions Pelican"

        # 4) Generated site evidence (GitHub Pages output)
        html_candidates = [root / "index.html"] + list(root.glob("**/index.html"))[:20]
        for p in html_candidates:
            if not p.exists():
                continue
            txt = _read_text(p).lower()
            if 'name="generator"' in txt and "pelican" in txt:
                return True, f"{p.relative_to(root)} contains Pelican generator meta tag"
            if "powered by pelican" in txt:
                return True, f"{p.relative_to(root)} contains 'Powered by Pelican'"

    except Exception:
        pass

    return False, None
