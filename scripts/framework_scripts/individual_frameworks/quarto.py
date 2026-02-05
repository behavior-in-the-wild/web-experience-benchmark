from pathlib import Path

# ------------------------
# Quarto detection constants
# ------------------------

QUARTO_CONFIG_FILES = ["_quarto.yml", "_quarto.yaml"]

# ------------------------
# Quarto detection
# ------------------------

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def detect_quarto(root: Path):
    try:
        # 1) Source config
        for cfg in QUARTO_CONFIG_FILES:
            if (root / cfg).exists():
                return True, f"Found Quarto config file: {cfg}"

        # 2) Generated site signature
        html_candidates = [root / "index.html"] + list(root.glob("**/index.html"))[:20]
        for p in html_candidates:
            if not p.exists():
                continue
            txt = _read_text(p).lower()

            # generator meta or embedded assets
            if "quarto" in txt and 'name="generator"' in txt:
                return True, f"{p.relative_to(root)} contains Quarto generator meta tag"

            # common footer / script markers
            if "quarto-html" in txt or "quarto-nav" in txt:
                return True, f"{p.relative_to(root)} contains Quarto HTML assets"

    except Exception:
        pass

    return False, None
