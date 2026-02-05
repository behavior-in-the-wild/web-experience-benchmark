from pathlib import Path

# ------------------------
# Vue.js detection constants
# ------------------------

VUE_TOOLCHAIN_DEPS = [
    "@vue/cli-service",       # Vue CLI
    "@vitejs/plugin-vue",     # Vite Vue
]

VUE_TOOLCHAIN_SCRIPT_TOKENS = [
    "vue-cli-service",
    "vite",
    "webpack",
    "parcel",
]

# ------------------------
# Vue.js detection (Vue SPA, excluding Nuxt)
# ------------------------

def _read_json(path: Path):
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def detect_vue(root: Path):
    try:
        pkg_candidates = [root / "package.json"] + list(root.glob("*/package.json"))

        for pkg_path in pkg_candidates:
            if not pkg_path.exists():
                continue

            pkg = _read_json(pkg_path)
            if not pkg:
                continue

            deps = pkg.get("dependencies", {}) or {}
            dev = pkg.get("devDependencies", {}) or {}

            # Exclude Nuxt
            if "nuxt" in deps or "nuxt" in dev:
                continue

            if "vue" not in deps and "vue" not in dev:
                continue

            rel_pkg = pkg_path.relative_to(root).as_posix()

            has_toolchain_dep = any(d in deps or d in dev for d in VUE_TOOLCHAIN_DEPS)

            scripts = pkg.get("scripts", {}) or {}
            scripts_text = " ".join(str(v).lower() for v in scripts.values())
            has_toolchain_script = any(tok in scripts_text for tok in VUE_TOOLCHAIN_SCRIPT_TOKENS)

            if has_toolchain_dep:
                return True, f"{rel_pkg} declares 'vue' with Vue SPA toolchain dependency"

            if has_toolchain_script:
                return True, f"{rel_pkg} declares 'vue' and scripts indicate SPA bundler"

            # Fallback: vue dependency alone (common in GitHub Pages SPAs)
            return True, f"{rel_pkg} declares dependency 'vue'"

    except Exception:
        pass

    return False, None
