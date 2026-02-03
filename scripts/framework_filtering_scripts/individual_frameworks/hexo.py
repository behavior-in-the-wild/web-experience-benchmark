from pathlib import Path

# ------------------------
# Hexo detection (index.html ONLY)
# ------------------------

HEX0_META_PREFIX = '<meta name="generator" content="hexo'  # already lowercase
HEX0_KEYWORDS = [
    "powered by hexo",
    "由 hexo",
    "hexo"
]

def detect_hexo(root: Path):
    index_html = root / "index.html"
    if not index_html.exists():
        return False, None

    try:
        text = index_html.read_text(errors="ignore").lower()
    except Exception:
        return False, None

    if HEX0_META_PREFIX in text:
        return True, "index.html contains <meta name='generator' content='Hexo...'>"

    for kw in HEX0_KEYWORDS:
        if kw in text:
            return True, f"index.html contains keyword '{kw}'"

    return False, None
