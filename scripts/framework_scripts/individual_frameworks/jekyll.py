from pathlib import Path

# ------------------------
# Jekyll detection (Gemfile / _config.yml ONLY)
# ------------------------

def detect_jekyll(root: Path):
    try:
        gemfile = root / "Gemfile"
        config_yml = root / "_config.yml"

        if gemfile.exists():
            txt = gemfile.read_text(encoding="utf-8", errors="ignore").lower()
            if "jekyll" in txt:
                return True, "Gemfile references 'jekyll'"

        if config_yml.exists():
            txt = config_yml.read_text(encoding="utf-8", errors="ignore").lower()
            if "jekyll" in txt:
                return True, "_config.yml references 'jekyll'"

    except Exception:
        pass

    return False, None
