from pathlib import Path

# ------------------------
# Flask detection constants
# ------------------------

FLASK_CODE_MARKERS = [
    "from flask import",
    "import flask",
    "flask(__name__)",
    "app = flask",
]

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")

# ------------------------
# Flask detection
# ------------------------

def detect_flask(root: Path):
    try:
        app_py = root / "app.py"
        if app_py.exists():
            txt = _read_text(app_py).lower()
            if any(m in txt for m in FLASK_CODE_MARKERS):
                return True, "app.py contains Flask application code"

        # requirements.txt
        req = root / "requirements.txt"
        if req.exists():
            txt = _read_text(req).lower()
            if "flask" in txt:
                return True, "requirements.txt declares 'flask'"

        # pyproject.toml (modern Python repos)
        pyproj = root / "pyproject.toml"
        if pyproj.exists():
            txt = _read_text(pyproj).lower()
            if "flask" in txt:
                return True, "pyproject.toml declares 'flask'"

    except Exception:
        pass

    return False, None
