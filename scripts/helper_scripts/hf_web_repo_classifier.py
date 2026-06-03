#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import subprocess
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Iterable, Any

from datasets import load_dataset


# =========================
# Configuration
# =========================

README_GLOBS = [
    "README*",
    "readme*",
]

EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".env",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
}

STATIC_HTML_EXCLUDED_DIRS = {
    "docs",
    "documentation",
    "test",
    "tests",
    "__tests__",
    "spec",
    "examples",
    "example",
    "demo",
    "demos",
    "storybook-static",
    "coverage",
    ".next",
    "dist",
    "build",
    "out",
    "_site",
    "site",
}

UI_SOURCE_EXTENSIONS = {
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".jsx",
    ".tsx",
    ".vue",
    ".svelte",
    ".astro",
}

UI_TEMPLATE_EXTENSIONS = {
    ".html",
    ".jinja",
    ".jinja2",
    ".j2",
    ".ejs",
    ".pug",
    ".hbs",
    ".handlebars",
    ".mustache",
    ".njk",
    ".twig",
    ".php",
    ".erb",
    ".haml",
    ".slim",
    ".ftl",
}

ASSET_EXTENSIONS = {
    ".css",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".webp",
}

DOC_EXTENSIONS = {".md", ".rst", ".adoc"}

GENERATED_DIRS = {
    "dist",
    "build",
    "out",
    "coverage",
    ".next",
    ".nuxt",
    ".svelte-kit",
    "storybook-static",
    "site",
    "_site",
    "public/build",
}

FRONTEND_DEPENDENCIES = {
    "react",
    "react-dom",
    "next",
    "vue",
    "nuxt",
    "@angular/core",
    "@angular/cli",
    "svelte",
    "@sveltejs/kit",
    "astro",
    "gatsby",
    "solid-js",
    "solid-start",
    "preact",
    "@builder.io/qwik",
    "lit",
    "ember-source",
    "backbone",
    "alpinejs",
    "stimulus",
    "@hotwired/turbo",
    "vite",
    "webpack",
    "parcel",
    "rollup",
    "snowpack",
    "react-scripts",
}

NODE_BACKEND_DEPENDENCIES = {
    "express",
    "koa",
    "hapi",
    "@hapi/hapi",
    "nestjs",
    "@nestjs/core",
    "fastify",
    "socket.io",
    "typeorm",
    "mongoose",
    "prisma",
}

NODE_TEMPLATE_DEPENDENCIES = {
    "ejs",
    "pug",
    "hbs",
    "handlebars",
    "nunjucks",
}

NODE_FRONTEND_SCRIPT_KEYS = {"dev", "start", "serve", "preview", "build"}

NODE_FRONTEND_SCRIPT_PATTERNS = [
    "vite",
    "next",
    "nuxt",
    "astro",
    "gatsby",
    "svelte-kit",
    "react-scripts",
    "webpack serve",
    "webpack-dev-server",
    "parcel",
    "ng serve",
    "remix",
    "storybook",
    "solid-start",
    "qwik",
    "serve",
    "http-server",
    "live-server",
]

NODE_RUN_SCRIPT_PATTERNS = [
    "vite",
    "next dev",
    "next start",
    "nuxt",
    "astro dev",
    "astro preview",
    "gatsby develop",
    "gatsby serve",
    "react-scripts start",
    "ng serve",
    "webpack serve",
    "webpack-dev-server",
    "parcel",
    "serve ",
    "http-server",
    "live-server",
    "remix dev",
    "node server.js",
    "node app.js",
    "node index.js",
    "npm run build &&",
    "pnpm dev",
    "yarn dev",
    "bun run",
]

README_INSTALL_PATTERNS = [
    "npm install",
    "npm i",
    "pnpm install",
    "yarn install",
    "bun install",
    "pip install -r requirements.txt",
    "pip install .",
    "poetry install",
    "bundle install",
    "composer install",
]

README_RUN_PATTERNS = [
    "npm run dev",
    "npm start",
    "npm run start",
    "npm run serve",
    "npm run preview",
    "pnpm dev",
    "yarn dev",
    "bun run dev",
    "python manage.py runserver",
    "flask run",
    "uvicorn ",
    "bundle exec jekyll serve",
    "hugo server",
    "php artisan serve",
    "php -S ",
    "docker compose up",
    "docker-compose up",
]

MOBILE_SIGNAL_FILES = {
    "AndroidManifest.xml",
    "app/src/main/AndroidManifest.xml",
    "Info.plist",
    "AppDelegate.swift",
    "SceneDelegate.swift",
    "metro.config.js",
    "app.json",
    "ionic.config.json",
    "config.xml",
    "capacitor.config.ts",
    "capacitor.config.json",
    "cordova.js",
    "pubspec.yaml",
    "lib/main.dart",
}

MOBILE_SIGNAL_DIRS = {
    ".xcodeproj",
    ".xcworkspace",
    "res/layout",
    "app/src/main/java",
    "app/src/main/kotlin",
    "android",
    "ios",
}

LIBRARY_README_KEYWORDS = {
    "library",
    "sdk",
    "package",
    "plugin",
    "wrapper",
    "client",
    "toolkit",
    "framework",
}

SCAFFOLD_README_KEYWORDS = {
    "starter",
    "boilerplate",
    "template",
    "scaffold",
    "seed",
}

FORK_ARCHIVE_README_PATTERNS = {
    "mirror of",
    "read-only mirror",
    "archived",
    "generated automatically",
}

DOCS_CONFIG_FILES = {
    "mkdocs.yml",
    "mkdocs.yaml",
    "docusaurus.config.js",
    "docusaurus.config.ts",
}

EXAMPLE_DIRS = {
    "example",
    "examples",
    "demo",
    "demos",
    "test",
    "tests",
    "__tests__",
    "spec",
    "fixtures",
    ".storybook",
    "storybook-static",
}

FRONTEND_CONFIG_FILES = {
    "vite.config.js",
    "vite.config.mjs",
    "vite.config.cjs",
    "vite.config.ts",
    "vite.config.mts",
    "vite.config.cts",
    "webpack.config.js",
    "webpack.config.mjs",
    "webpack.config.cjs",
    "webpack.config.ts",
    "webpack.dev.js",
    "webpack.prod.js",
    "parcelrc",
    ".parcelrc",
    "rollup.config.js",
    "rollup.config.mjs",
    "rollup.config.ts",
    "snowpack.config.js",
    "snowpack.config.mjs",
    "snowpack.config.cjs",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "nuxt.config.js",
    "nuxt.config.ts",
    "astro.config.js",
    "astro.config.mjs",
    "astro.config.cjs",
    "astro.config.ts",
    "astro.config.mts",
    "svelte.config.js",
    "svelte.config.cjs",
    "svelte.config.mjs",
    "svelte.config.ts",
    "angular.json",
    ".angular-cli.json",
    "gatsby-config.js",
    "gatsby-config.mjs",
    "gatsby-config.ts",
    "gatsby-node.js",
    "gatsby-browser.js",
    "gatsby-ssr.js",
    "remix.config.js",
    "remix.config.mjs",
    "remix.config.ts",
    "app.config.ts",
}

STATIC_SITE_CONFIG_FILES = {
    "_config.yml",
    "_config.yaml",
    "hugo.toml",
    "hugo.yaml",
    "hugo.yml",
    "config.toml",
    "config.yaml",
    "config.yml",
    ".eleventy.js",
    "eleventy.config.js",
    "eleventy.config.cjs",
    "eleventy.config.mjs",
    "docusaurus.config.js",
    "docusaurus.config.ts",
    "mkdocs.yml",
    "mkdocs.yaml",
}

ROOT_HTML_ENTRY_FILES = {
    "index.html",
    "index.htm",
    "default.html",
    "default.htm",
}


# =========================
# Data structures
# =========================

@dataclass
class RepoInput:
    source: str
    local_path: Optional[str] = None
    cloned: bool = False


@dataclass
class RepoFacts:
    repo_root: str
    files: List[str] = field(default_factory=list)
    dirs: List[str] = field(default_factory=list)
    root_files: Set[str] = field(default_factory=set)
    root_dirs: Set[str] = field(default_factory=set)
    extension_counts: Dict[str, int] = field(default_factory=dict)

    package_json: Optional[Dict[str, Any]] = None
    composer_json: Optional[Dict[str, Any]] = None
    pyproject_toml_text: str = ""
    requirements_txt_text: str = ""
    pipfile_text: str = ""
    gemfile_text: str = ""
    go_mod_text: str = ""
    pom_xml_text: str = ""
    build_gradle_text: str = ""
    build_gradle_kts_text: str = ""

    readme_text: str = ""
    dockerfile_texts: Dict[str, str] = field(default_factory=dict)
    compose_texts: Dict[str, str] = field(default_factory=dict)


@dataclass
class FilterResult:
    source: str
    local_path: str
    included: bool
    inclusion_signals: List[str] = field(default_factory=list)
    executability_signals: List[str] = field(default_factory=list)
    exclusion_signals: List[str] = field(default_factory=list)
    final_reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


# =========================
# Utility helpers
# =========================

def normalize_rel_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def safe_read_text(path: Path, max_bytes: int) -> str:
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def is_git_url(s: str) -> bool:
    s = s.strip()
    return (
        s.startswith("http://")
        or s.startswith("https://")
        or s.startswith("git@")
        or s.endswith(".git")
    )


def run_git_clone(url: str, destination: Path) -> bool:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(destination)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return True
    except Exception:
        return False


def find_matching_files(paths: Iterable[str], names: Set[str]) -> List[str]:
    names_lower = {n.lower() for n in names}
    return [p for p in paths if Path(p).name.lower() in names_lower]


def path_exists(files: Iterable[str], dirs: Iterable[str], target: str) -> bool:
    target = target.strip("/").lower()
    for p in files:
        if p.lower() == target:
            return True
    for d in dirs:
        if d.strip("/").lower() == target:
            return True
    return False


def has_any_path_prefix(paths: Iterable[str], prefixes: Iterable[str]) -> bool:
    norm_prefixes = [p.strip("/").lower() for p in prefixes]
    for path in paths:
        lp = path.lower()
        for prefix in norm_prefixes:
            if lp == prefix or lp.startswith(prefix + "/"):
                return True
    return False


def get_dependency_names_from_package_json(pkg: Optional[Dict[str, Any]]) -> Set[str]:
    if not pkg or not isinstance(pkg, dict):
        return set()

    names: Set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = pkg.get(key, {})
        if isinstance(value, dict):
            names.update(value.keys())
    return names


def get_package_json_scripts(pkg: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not pkg or not isinstance(pkg, dict):
        return {}
    scripts = pkg.get("scripts", {})
    if isinstance(scripts, dict):
        return {str(k): str(v) for k, v in scripts.items()}
    return {}


def text_contains_html_markup(text: str) -> bool:
    text_lower = text.lower()
    return any(token in text_lower for token in ("<html", "<!doctype html", "<body"))


def collect_readme_text(root: Path, max_bytes: int) -> str:
    texts: List[str] = []
    for pattern in README_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                texts.append(safe_read_text(path, max_bytes))
    return "\n".join(texts)


def top_level_component(path_str: str) -> str:
    parts = Path(path_str).parts
    return parts[0] if parts else ""


# =========================
# Repo scanning
# =========================

def scan_repo(repo_root: Path, max_file_read_bytes: int) -> RepoFacts:
    files: List[str] = []
    dirs_set: Set[str] = set()
    extension_counts: Counter[str] = Counter()

    for current_root, dirnames, filenames in os.walk(repo_root):
        current_root_path = Path(current_root)
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]

        rel_dir = normalize_rel_path(current_root_path, repo_root) if current_root_path != repo_root else ""
        if rel_dir:
            dirs_set.add(rel_dir)

        for d in dirnames:
            rel = normalize_rel_path(current_root_path / d, repo_root)
            dirs_set.add(rel)

        for fname in filenames:
            full_path = current_root_path / fname
            rel_file = normalize_rel_path(full_path, repo_root)
            files.append(rel_file)
            extension_counts[full_path.suffix.lower()] += 1

    root_files = {Path(f).name for f in files if "/" not in f}
    root_dirs = {Path(d).name for d in dirs_set if "/" not in d}

    facts = RepoFacts(
        repo_root=str(repo_root),
        files=sorted(files),
        dirs=sorted(dirs_set),
        root_files=root_files,
        root_dirs=root_dirs,
        extension_counts=dict(extension_counts),
    )

    pkg_path = repo_root / "package.json"
    facts.package_json = load_json_file(pkg_path) if pkg_path.exists() else None

    composer_path = repo_root / "composer.json"
    facts.composer_json = load_json_file(composer_path) if composer_path.exists() else None

    special_files = {
        "pyproject.toml": "pyproject_toml_text",
        "requirements.txt": "requirements_txt_text",
        "Pipfile": "pipfile_text",
        "Gemfile": "gemfile_text",
        "go.mod": "go_mod_text",
        "pom.xml": "pom_xml_text",
        "build.gradle": "build_gradle_text",
        "build.gradle.kts": "build_gradle_kts_text",
    }

    for fname, attr in special_files.items():
        path = repo_root / fname
        if path.exists():
            setattr(facts, attr, safe_read_text(path, max_file_read_bytes))

    facts.readme_text = collect_readme_text(repo_root, max_file_read_bytes)

    for file_rel in facts.files:
        file_name = Path(file_rel).name.lower()
        if file_name == "dockerfile" or file_name.startswith("dockerfile."):
            facts.dockerfile_texts[file_rel] = safe_read_text(repo_root / file_rel, max_file_read_bytes)

        if file_name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}:
            facts.compose_texts[file_rel] = safe_read_text(repo_root / file_rel, max_file_read_bytes)

    return facts


# =========================
# Signal detection helpers
# =========================

def package_has_frontend_dependency(facts: RepoFacts) -> bool:
    deps = get_dependency_names_from_package_json(facts.package_json)
    return bool(deps.intersection(FRONTEND_DEPENDENCIES))


def package_has_backend_only_node_dependency_set(facts: RepoFacts) -> bool:
    deps = get_dependency_names_from_package_json(facts.package_json)
    if not deps:
        return False
    has_backend = bool(deps.intersection(NODE_BACKEND_DEPENDENCIES))
    has_frontend = bool(deps.intersection(FRONTEND_DEPENDENCIES))
    return has_backend and not has_frontend


def package_has_node_template_dependency(facts: RepoFacts) -> bool:
    deps = get_dependency_names_from_package_json(facts.package_json)
    return bool(deps.intersection(NODE_TEMPLATE_DEPENDENCIES))


def package_has_frontend_scripts(facts: RepoFacts) -> bool:
    scripts = get_package_json_scripts(facts.package_json)
    for key, value in scripts.items():
        if key in NODE_FRONTEND_SCRIPT_KEYS:
            value_lower = value.lower()
            if any(pat.lower() in value_lower for pat in NODE_FRONTEND_SCRIPT_PATTERNS):
                return True
    return False


def package_has_run_script(facts: RepoFacts) -> bool:
    scripts = get_package_json_scripts(facts.package_json)
    for key, value in scripts.items():
        if key in {"dev", "start", "serve", "preview"}:
            value_lower = value.lower()
            if any(p.lower() in value_lower for p in NODE_RUN_SCRIPT_PATTERNS):
                return True
    return False


def package_has_lock_or_dependencies(facts: RepoFacts) -> bool:
    if any(name in facts.root_files for name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock"}):
        return True
    if facts.package_json:
        deps = get_dependency_names_from_package_json(facts.package_json)
        return len(deps) > 0
    return False


def repo_has_frontend_source_layout(facts: RepoFacts) -> bool:
    layout_dirs = {"src", "public", "pages", "app", "components"}
    has_dir = any(d in facts.root_dirs or has_any_path_prefix(facts.dirs, [d]) for d in layout_dirs)
    has_source_file = any(Path(f).suffix.lower() in {".jsx", ".tsx", ".vue", ".svelte", ".astro", ".html"} for f in facts.files)
    return has_dir and has_source_file


def repo_has_templates_dir(facts: RepoFacts) -> bool:
    return has_any_path_prefix(facts.dirs, ["templates", "app/views", "src/main/resources/templates", "views"])


def repo_has_static_dir(facts: RepoFacts) -> bool:
    return has_any_path_prefix(facts.dirs, ["static", "public", "app/assets", "src/main/resources/static", "assets"])


def repo_has_html_templates(facts: RepoFacts) -> bool:
    for f in facts.files:
        lower_f = f.lower()
        if (
            "/templates/" in lower_f
            or lower_f.startswith("templates/")
            or "/views/" in lower_f
            or lower_f.startswith("views/")
            or "src/main/resources/templates/" in lower_f
        ):
            if Path(lower_f).suffix.lower() in UI_TEMPLATE_EXTENSIONS:
                return True
    return False


def readme_has_install_and_run(facts: RepoFacts) -> bool:
    text = facts.readme_text.lower()
    has_install = any(p.lower() in text for p in README_INSTALL_PATTERNS)
    has_run = any(p.lower() in text for p in README_RUN_PATTERNS)
    return has_install and has_run


def file_content_contains(repo_root: Path, rel_path: str, needle_patterns: List[str], max_bytes: int) -> bool:
    text = safe_read_text(repo_root / rel_path, max_bytes).lower()
    return any(p.lower() in text for p in needle_patterns)


def requirements_contain(facts: RepoFacts, patterns: List[str]) -> bool:
    joined = "\n".join([
        facts.requirements_txt_text,
        facts.pyproject_toml_text,
        facts.pipfile_text,
    ]).lower()
    return any(p.lower() in joined for p in patterns)


def gemfile_contains(facts: RepoFacts, patterns: List[str]) -> bool:
    text = facts.gemfile_text.lower()
    return any(p.lower() in text for p in patterns)


def composer_contains(facts: RepoFacts, patterns: List[str]) -> bool:
    if not facts.composer_json:
        return False
    raw = json.dumps(facts.composer_json).lower()
    return any(p.lower() in raw for p in patterns)


def gradle_or_pom_contains(facts: RepoFacts, patterns: List[str]) -> bool:
    text = "\n".join([facts.pom_xml_text, facts.build_gradle_text, facts.build_gradle_kts_text]).lower()
    return any(p.lower() in text for p in patterns)


# =========================
# Inclusion signal detection
# =========================

def detect_inclusion_signals(facts: RepoFacts, max_bytes: int) -> List[str]:
    signals: List[str] = []
    repo_root = Path(facts.repo_root)

    if any(name in facts.root_files for name in ROOT_HTML_ENTRY_FILES):
        signals.append("F1_root_html_entrypoint")

    html_files = [f for f in facts.files if Path(f).suffix.lower() in {".html", ".htm"}]
    html_files_outside_excluded = []
    for f in html_files:
        parts = Path(f).parts
        if not any(part.lower() in STATIC_HTML_EXCLUDED_DIRS for part in parts):
            html_files_outside_excluded.append(f)

    has_asset_file = any(Path(f).suffix.lower() in ASSET_EXTENSIONS for f in facts.files)
    if len(html_files) >= 3 and len(html_files_outside_excluded) >= 1 and has_asset_file:
        signals.append("F2_multi_page_static_site")

    if facts.package_json:
        frontend_dep = package_has_frontend_dependency(facts)
        frontend_scripts = package_has_frontend_scripts(facts)
        frontend_layout = repo_has_frontend_source_layout(facts)

        if frontend_dep:
            signals.append("F3_1_package_json_frontend_dependency")
        if frontend_scripts:
            signals.append("F3_2_package_json_frontend_script")
        if frontend_layout:
            signals.append("F3_3_frontend_source_layout")

        if frontend_dep or frontend_scripts or frontend_layout:
            signals.append("F3_node_frontend_manifest_group")

    config_files_present = set(find_matching_files(facts.files, FRONTEND_CONFIG_FILES))
    if config_files_present:
        config_names = {Path(f).name for f in config_files_present}

        if {"next.config.js", "next.config.mjs", "next.config.ts"}.intersection(config_names):
            if has_any_path_prefix(facts.dirs, ["pages", "app", "src/pages", "src/app"]):
                signals.append("F4_nextjs_config_and_structure")

        if {"nuxt.config.js", "nuxt.config.ts"}.intersection(config_names):
            if has_any_path_prefix(facts.dirs, ["pages", "layouts"]) or path_exists(facts.files, facts.dirs, "app.vue"):
                signals.append("F4_nuxt_config_and_structure")

        if {"astro.config.js", "astro.config.mjs", "astro.config.cjs", "astro.config.ts", "astro.config.mts"}.intersection(config_names):
            if has_any_path_prefix(facts.dirs, ["src/pages", "src/components", "public"]):
                signals.append("F4_astro_config_and_structure")

        if {"svelte.config.js", "svelte.config.cjs", "svelte.config.mjs", "svelte.config.ts"}.intersection(config_names):
            if has_any_path_prefix(facts.dirs, ["src/routes", "src/lib"]) or path_exists(facts.files, facts.dirs, "src/app.html"):
                signals.append("F4_svelte_config_and_structure")

        if "angular.json" in config_names or ".angular-cli.json" in config_names:
            if has_any_path_prefix(facts.dirs, ["src/app"]) or path_exists(facts.files, facts.dirs, "src/main.ts") or path_exists(facts.files, facts.dirs, "src/index.html"):
                signals.append("F4_angular_config_and_structure")

        if {"gatsby-config.js", "gatsby-config.mjs", "gatsby-config.ts", "gatsby-node.js", "gatsby-browser.js", "gatsby-ssr.js"}.intersection(config_names):
            if has_any_path_prefix(facts.dirs, ["src/pages", "src/components"]):
                signals.append("F4_gatsby_config_and_structure")

        if {"remix.config.js", "remix.config.mjs", "remix.config.ts"}.intersection(config_names):
            if has_any_path_prefix(facts.dirs, ["app", "app/routes"]):
                signals.append("F4_remix_config_and_structure")

        if {"vite.config.js", "vite.config.mjs", "vite.config.cjs", "vite.config.ts", "vite.config.mts", "vite.config.cts"}.intersection(config_names):
            has_ui_source = any(Path(f).suffix.lower() in {".jsx", ".tsx", ".vue", ".svelte", ".astro"} for f in facts.files)
            if has_any_path_prefix(facts.dirs, ["src"]) and has_ui_source:
                signals.append("F4_vite_config_and_structure")

        if "app.config.ts" in config_names and has_any_path_prefix(facts.dirs, ["src/routes"]):
            signals.append("F4_solidstart_or_qwik_like_config_and_structure")

    static_cfg_present = set(find_matching_files(facts.files, STATIC_SITE_CONFIG_FILES))
    if static_cfg_present:
        names = {Path(f).name for f in static_cfg_present}

        if {"_config.yml", "_config.yaml"}.intersection(names):
            if has_any_path_prefix(facts.dirs, ["_layouts", "_includes", "_posts", "assets"]):
                signals.append("F5_jekyll_config_and_structure")
            elif has_any_path_prefix(facts.dirs, ["scaffolds", "themes", "source"]):
                signals.append("F5_hexo_config_and_structure")

        if {"hugo.toml", "hugo.yaml", "hugo.yml"}.intersection(names) or (
            {"config.toml", "config.yaml", "config.yml"}.intersection(names)
            and has_any_path_prefix(facts.dirs, ["content", "layouts", "themes", "archetypes", "static", "config/_default"])
        ):
            signals.append("F5_hugo_config_and_structure")

        if {".eleventy.js", "eleventy.config.js", "eleventy.config.cjs", "eleventy.config.mjs"}.intersection(names):
            if has_any_path_prefix(facts.dirs, ["_includes", "_data", "src", "content"]):
                signals.append("F5_eleventy_config_and_structure")

        if {"docusaurus.config.js", "docusaurus.config.ts"}.intersection(names):
            if has_any_path_prefix(facts.dirs, ["docs", "blog", "src/pages", "src/components"]):
                signals.append("F5_docusaurus_config_and_structure")

        if {"mkdocs.yml", "mkdocs.yaml"}.intersection(names) and has_any_path_prefix(facts.dirs, ["docs"]):
            signals.append("F5_mkdocs_config_and_structure")

    if (
        path_exists(facts.files, facts.dirs, ".vuepress/config.js")
        or path_exists(facts.files, facts.dirs, ".vuepress/config.ts")
        or path_exists(facts.files, facts.dirs, "docs/.vuepress/config.js")
        or path_exists(facts.files, facts.dirs, "docs/.vuepress/config.ts")
    ):
        signals.append("F5_vuepress_config_and_structure")

    has_templates = repo_has_templates_dir(facts)
    has_static = repo_has_static_dir(facts)
    has_html_templates = repo_has_html_templates(facts)

    if requirements_contain(facts, ["django"]) and path_exists(facts.files, facts.dirs, "manage.py"):
        if has_templates and has_html_templates and has_static:
            signals.append("F6_django_templates_static_site")

    if requirements_contain(facts, ["flask"]) and (
        path_exists(facts.files, facts.dirs, "app.py")
        or path_exists(facts.files, facts.dirs, "wsgi.py")
        or path_exists(facts.files, facts.dirs, "main.py")
    ):
        if has_templates and has_html_templates and has_static:
            signals.append("F6_flask_templates_static_site")

    if requirements_contain(facts, ["fastapi", "starlette"]) and (
        has_templates or has_html_templates or requirements_contain(facts, ["jinja2"])
    ):
        fastapi_ui_signal = False
        for f in facts.files:
            if Path(f).suffix.lower() == ".py":
                if file_content_contains(repo_root, f, ["Jinja2Templates", "StaticFiles"], max_bytes):
                    fastapi_ui_signal = True
                    break
        if fastapi_ui_signal or has_html_templates:
            signals.append("F6_fastapi_with_templates_or_static")

    if gemfile_contains(facts, ["rails"]) and has_any_path_prefix(facts.dirs, ["app/views"]) and path_exists(facts.files, facts.dirs, "config/routes.rb"):
        if has_any_path_prefix(facts.dirs, ["app/assets", "public", "app/javascript"]):
            signals.append("F7_rails_views_assets_site")

    if composer_contains(facts, ["laravel/framework"]) and path_exists(facts.files, facts.dirs, "artisan") \
       and has_any_path_prefix(facts.dirs, ["resources/views"]) and path_exists(facts.files, facts.dirs, "routes/web.php"):
        signals.append("F8_laravel_views_routes_site")
    else:
        php_files = [f for f in facts.files if Path(f).suffix.lower() == ".php"]
        if len(php_files) >= 3 and (
            path_exists(facts.files, facts.dirs, "index.php")
            or path_exists(facts.files, facts.dirs, "public/index.php")
        ):
            htmlish = False
            for f in php_files[:20]:
                if text_contains_html_markup(safe_read_text(repo_root / f, max_bytes)):
                    htmlish = True
                    break
            if htmlish and has_any_path_prefix(facts.dirs, ["assets", "css", "js", "public"]):
                signals.append("F8_plain_php_site")

    if gradle_or_pom_contains(
        facts,
        [
            "spring-boot-starter-web",
            "spring-boot-starter-thymeleaf",
            "spring-boot-starter-mustache",
            "spring-boot-starter-freemarker",
        ],
    ):
        if has_any_path_prefix(facts.dirs, ["src/main/resources/templates", "src/main/resources/static"]) or repo_has_html_templates(facts):
            signals.append("F9_spring_server_rendered_site")

    if facts.go_mod_text:
        go_http_imports = False
        for f in facts.files:
            if Path(f).suffix.lower() == ".go":
                if file_content_contains(
                    repo_root,
                    f,
                    [
                        '"net/http"',
                        '"github.com/gin-gonic/gin"',
                        '"github.com/labstack/echo"',
                        '"github.com/gofiber/fiber"',
                        '"github.com/go-chi/chi"',
                    ],
                    max_bytes,
                ):
                    go_http_imports = True
                    break
        if go_http_imports and has_any_path_prefix(facts.dirs, ["templates", "views", "static", "public"]) and repo_has_html_templates(facts):
            signals.append("F10_go_templates_static_site")

    return signals


# =========================
# Executability signal detection
# =========================

def detect_executability_signals(facts: RepoFacts) -> List[str]:
    signals: List[str] = []

    if facts.package_json:
        if package_has_lock_or_dependencies(facts):
            signals.append("E1_1_node_dependency_manifest_present")
        scripts = get_package_json_scripts(facts.package_json)
        if scripts and any(k in scripts for k in {"dev", "start", "serve", "preview"}):
            signals.append("E1_2_node_run_script_key_present")
        if package_has_run_script(facts):
            signals.append("E1_3_node_plausible_local_launcher")
        if (
            "E1_1_node_dependency_manifest_present" in signals
            and "E1_2_node_run_script_key_present" in signals
            and "E1_3_node_plausible_local_launcher" in signals
        ):
            signals.append("E1_node_install_and_run_capability")

    if facts.requirements_txt_text or facts.pyproject_toml_text or facts.pipfile_text:
        if path_exists(facts.files, facts.dirs, "manage.py") or "python manage.py runserver" in facts.readme_text.lower() or "django-admin runserver" in facts.readme_text.lower():
            signals.append("E2_django_local_launcher")
        if ("flask run" in facts.readme_text.lower() or "app.run(" in facts.readme_text.lower() or "flask_app=" in facts.readme_text.lower()) and (
            path_exists(facts.files, facts.dirs, "app.py")
            or path_exists(facts.files, facts.dirs, "main.py")
            or path_exists(facts.files, facts.dirs, "wsgi.py")
        ):
            signals.append("E2_flask_local_launcher")
        if "uvicorn " in facts.readme_text.lower() or "hypercorn " in facts.readme_text.lower():
            signals.append("E2_asgi_local_launcher")

    if facts.gemfile_text:
        if path_exists(facts.files, facts.dirs, "bin/rails") or "rails server" in facts.readme_text.lower() or "bin/rails server" in facts.readme_text.lower():
            signals.append("E3_rails_local_launcher")
        if gemfile_contains(facts, ["jekyll"]) or "bundle exec jekyll serve" in facts.readme_text.lower():
            signals.append("E3_jekyll_local_launcher")

    if "php -s" in facts.readme_text.lower() or "php artisan serve" in facts.readme_text.lower() or path_exists(facts.files, facts.dirs, "artisan") or composer_contains(facts, ["laravel/framework"]):
        signals.append("E4_php_local_launcher")

    dockerish = False
    if facts.dockerfile_texts or facts.compose_texts:
        all_docker_text = "\n".join(list(facts.dockerfile_texts.values()) + list(facts.compose_texts.values())).lower()
        if any(token.lower() in all_docker_text for token in [
            "expose ",
            "cmd ",
            "entrypoint ",
            "ports:",
            "nginx",
            "apache",
            "node",
            "npm",
            "yarn",
            "pnpm",
            "python",
            "gunicorn",
            "uvicorn",
            "php-fpm",
            "caddy",
        ]):
            dockerish = True
    if dockerish:
        signals.append("E5_dockerized_launcher")

    if readme_has_install_and_run(facts):
        signals.append("E6_readme_install_and_run_instructions")

    return signals


# =========================
# Exclusion signal detection
# =========================

def detect_exclusion_signals(facts: RepoFacts, inclusion_signals: List[str], max_bytes: int) -> List[str]:
    signals: List[str] = []
    repo_root = Path(facts.repo_root)
    inclusion_set = set(inclusion_signals)

    mobile_file_hit = any(path_exists(facts.files, facts.dirs, f) for f in MOBILE_SIGNAL_FILES)
    mobile_dir_hit = any(has_any_path_prefix(facts.dirs, [d]) for d in MOBILE_SIGNAL_DIRS)
    react_native_hit = False
    if facts.package_json:
        deps = get_dependency_names_from_package_json(facts.package_json)
        if "react-native" in deps or "expo" in deps:
            react_native_hit = True

    gradle_android_hit = "com.android.application" in facts.build_gradle_text.lower() or "com.android.application" in facts.build_gradle_kts_text.lower()
    storyboard_or_xib = any(Path(f).suffix.lower() in {".storyboard", ".xib"} for f in facts.files)

    mobile_hit = mobile_file_hit or mobile_dir_hit or react_native_hit or gradle_android_hit or storyboard_or_xib
    if mobile_hit:
        web_explicit = (
            path_exists(facts.files, facts.dirs, "web/index.html")
            or "flutter build web" in facts.readme_text.lower()
            or "flutter run -d chrome" in facts.readme_text.lower()
            or bool(inclusion_set)
        )
        if not web_explicit:
            signals.append("X1_mobile_only_repository")

    backend_only = False

    if facts.package_json and package_has_backend_only_node_dependency_set(facts):
        has_backend_dirs = has_any_path_prefix(facts.dirs, ["routes", "controllers", "middlewares", "models", "prisma"])
        has_ui_dirs = has_any_path_prefix(facts.dirs, ["public", "views", "src", "pages", "app"])
        has_templates = repo_has_templates_dir(facts) or package_has_node_template_dependency(facts)
        if has_backend_dirs and not has_ui_dirs and not has_templates and not inclusion_set:
            backend_only = True

    if requirements_contain(facts, ["fastapi", "uvicorn", "flask", "django"]):
        if not repo_has_templates_dir(facts) and not repo_has_static_dir(facts) and not repo_has_html_templates(facts):
            has_jinja = requirements_contain(facts, ["jinja2"])
            if not has_jinja and not inclusion_set:
                backend_only = True

    if gradle_or_pom_contains(facts, ["spring-boot-starter-web"]):
        if not has_any_path_prefix(facts.dirs, ["src/main/resources/templates", "src/main/resources/static"]) and not inclusion_set:
            backend_only = True

    if facts.go_mod_text:
        has_http_framework = False
        for f in facts.files:
            if Path(f).suffix.lower() == ".go":
                if file_content_contains(
                    repo_root,
                    f,
                    [
                        '"net/http"',
                        '"github.com/gin-gonic/gin"',
                        '"github.com/labstack/echo"',
                        '"github.com/gofiber/fiber"',
                        '"github.com/go-chi/chi"',
                    ],
                    max_bytes,
                ):
                    has_http_framework = True
                    break
        if has_http_framework and not has_any_path_prefix(facts.dirs, ["templates", "views", "static", "public"]) and not inclusion_set:
            backend_only = True

    if backend_only:
        signals.append("X2_backend_only_repository")

    library_hit = False
    if facts.package_json:
        pkg = facts.package_json
        library_keys = {"main", "module", "types", "exports", "files", "publishConfig", "bin"}
        if any(k in pkg for k in library_keys):
            has_web_app_structure = any(sig.startswith("F") for sig in inclusion_signals)
            if not has_web_app_structure:
                library_hit = True

        readme_lower = facts.readme_text.lower()
        if any(word in readme_lower for word in LIBRARY_README_KEYWORDS):
            if not inclusion_set:
                library_hit = True

    if facts.pyproject_toml_text or path_exists(facts.files, facts.dirs, "setup.py"):
        if not repo_has_templates_dir(facts) and not repo_has_static_dir(facts) and not inclusion_set:
            library_hit = True

    if (facts.pom_xml_text or facts.build_gradle_text or facts.build_gradle_kts_text) and has_any_path_prefix(facts.dirs, ["src/main/java"]) \
       and not has_any_path_prefix(facts.dirs, ["src/main/resources/templates", "src/main/resources/static"]) and not inclusion_set:
        library_hit = True

    if library_hit:
        signals.append("X3_library_sdk_package_repository")

    docs_only = False
    doc_file_count = sum(v for ext, v in facts.extension_counts.items() if ext in DOC_EXTENSIONS)
    non_doc_ui_count = sum(v for ext, v in facts.extension_counts.items() if ext in UI_SOURCE_EXTENSIONS)
    docs_structure = (
        has_any_path_prefix(facts.dirs, ["docs", "documentation"])
        or any(path_exists(facts.files, facts.dirs, cfg) for cfg in DOCS_CONFIG_FILES)
        or path_exists(facts.files, facts.dirs, "docs/.vuepress/config.js")
        or path_exists(facts.files, facts.dirs, "docs/.vuepress/config.ts")
    )
    if docs_structure and non_doc_ui_count == 0 and not inclusion_set:
        docs_only = True
    elif docs_structure and doc_file_count > 0 and not any(
        sig for sig in inclusion_signals
        if not sig.startswith("F5_mkdocs") and not sig.startswith("F5_docusaurus") and not sig.startswith("F5_vuepress")
    ):
        docs_only = True

    if docs_only:
        signals.append("X4_documentation_only_repository")

    ipynb_count = facts.extension_counts.get(".ipynb", 0)
    ui_count = sum(v for ext, v in facts.extension_counts.items() if ext in UI_SOURCE_EXTENSIONS)
    if ipynb_count > 0 and ui_count == 0 and not repo_has_templates_dir(facts) and not facts.package_json:
        signals.append("X5_notebook_only_repository")

    cli_only = False
    if facts.package_json and "bin" in facts.package_json and not inclusion_set:
        cli_only = True

    py_text = "\n".join([facts.pyproject_toml_text, safe_read_text(Path(facts.repo_root) / "setup.py", 100_000)])
    if "console_scripts" in py_text.lower() and not inclusion_set:
        cli_only = True

    if has_any_path_prefix(facts.dirs, ["cmd", "cli", "bin"]) and not inclusion_set:
        cli_only = True

    if cli_only:
        signals.append("X6_cli_only_repository")

    scaffold_hit = False
    top_levels = Counter(top_level_component(f) for f in facts.files if top_level_component(f))
    exampleish_count = sum(count for name, count in top_levels.items() if name.lower() in EXAMPLE_DIRS)
    total_files = max(1, len(facts.files))
    if exampleish_count / total_files >= 0.5 and not inclusion_set:
        scaffold_hit = True

    if any(word in facts.readme_text.lower() for word in SCAFFOLD_README_KEYWORDS) and not inclusion_set:
        scaffold_hit = True

    if scaffold_hit:
        signals.append("X7_test_example_demo_scaffold_only_repository")

    generated_count = 0
    for f in facts.files:
        if any(f.lower() == gd or f.lower().startswith(gd + "/") for gd in GENERATED_DIRS):
            generated_count += 1

    has_source_tree = has_any_path_prefix(facts.dirs, ["src", "pages", "app", "content", "layouts", "templates"])
    if len(facts.files) > 0 and generated_count / len(facts.files) >= 0.6 and not has_source_tree:
        signals.append("X8_generated_or_build_artifact_only_repository")

    readme_lower = facts.readme_text.lower()
    if any(p in readme_lower for p in FORK_ARCHIVE_README_PATTERNS):
        signals.append("X9_fork_mirror_archive_repository")

    return signals


# =========================
# Final filtering
# =========================

def apply_filter(facts: RepoFacts, max_bytes: int) -> FilterResult:
    inclusion_signals = detect_inclusion_signals(facts, max_bytes)
    executability_signals = detect_executability_signals(facts)
    exclusion_signals = detect_exclusion_signals(facts, inclusion_signals, max_bytes)

    included = (
        len(inclusion_signals) > 0
        and len(executability_signals) > 0
        and len(exclusion_signals) == 0
    )

    if included:
        final_reason = "included: has website-candidacy signal(s), executability signal(s), and no exclusion signal"
    else:
        missing_parts = []
        if not inclusion_signals:
            missing_parts.append("missing inclusion signals")
        if not executability_signals:
            missing_parts.append("missing executability signals")
        if exclusion_signals:
            missing_parts.append("has exclusion signals")
        final_reason = "excluded: " + ", ".join(missing_parts)

    details = {
        "file_count": len(facts.files),
        "dir_count": len(facts.dirs),
        "root_files": sorted(facts.root_files),
        "root_dirs": sorted(facts.root_dirs),
        "extension_counts": facts.extension_counts,
    }

    return FilterResult(
        source=facts.repo_root,
        local_path=facts.repo_root,
        included=included,
        inclusion_signals=inclusion_signals,
        executability_signals=executability_signals,
        exclusion_signals=exclusion_signals,
        final_reason=final_reason,
        details=details,
    )


# =========================
# Repo preparation
# =========================

def repo_id_to_url(repo_id: str) -> str:
    repo_id = repo_id.strip().strip("/")
    return f"https://github.com/{repo_id}"


def repo_id_to_clone_dir(workdir: Path, repo_id: str) -> Path:
    repo_id = repo_id.strip().strip("/")
    parts = repo_id.split("/", 1)
    if len(parts) == 2:
        owner, repo = parts
        return workdir / owner / repo
    return workdir / repo_id.replace("/", "__")


def prepare_repo(source: str, workdir: Path) -> Optional[RepoInput]:
    source = source.strip()
    if not source:
        return None

    if is_git_url(source):
        clean = source.rstrip("/")
        if clean.endswith(".git"):
            clean = clean[:-4]

        # Expect https://github.com/owner/repo
        if "github.com/" in clean:
            repo_part = clean.split("github.com/", 1)[1].strip("/")
            target_dir = repo_id_to_clone_dir(workdir, repo_part)
        else:
            repo_name = Path(clean).name
            target_dir = workdir / repo_name

        if target_dir.exists():
            if target_dir.is_dir():
                return RepoInput(source=source, local_path=str(target_dir), cloned=False)
            return None

        ok = run_git_clone(source, target_dir)
        if not ok:
            return None
        return RepoInput(source=source, local_path=str(target_dir), cloned=True)

    local = Path(source).expanduser().resolve()
    if local.exists() and local.is_dir():
        return RepoInput(source=source, local_path=str(local), cloned=False)

    return None


# =========================
# HF streaming
# =========================

def stream_unique_repo_ids(dataset_name: str, split: str, hf_token: Optional[str] = None):
    ds = load_dataset(
        dataset_name,
        split=split,
        streaming=True,
        columns=["repo_id"],
        token=hf_token,
    )

    last_repo_id = None
    for row in ds:
        repo_id = row.get("repo_id")
        if not repo_id:
            continue
        if repo_id != last_repo_id:
            yield repo_id
            last_repo_id = repo_id


# =========================
# Worker
# =========================

def process_repo_id(
    repo_id: str,
    workdir_str: str,
    max_file_read_bytes: int,
    cleanup_clones: bool,
    debug: bool,
) -> Dict[str, Any]:
    repo_url = repo_id_to_url(repo_id)
    workdir = Path(workdir_str)
    prepared = None
    cloned_path: Optional[Path] = None

    try:
        prepared = prepare_repo(repo_url, workdir)
        if prepared is None:
            return {
                "repo_id": repo_id,
                "repo_url": repo_url,
                "source": repo_url,
                "local_path": "",
                "included": False,
                "inclusion_signals": [],
                "executability_signals": [],
                "exclusion_signals": ["INPUT_PREPARATION_FAILED"],
                "final_reason": "excluded: repository path invalid or clone failed",
                "details": {},
            }

        if prepared.cloned:
            cloned_path = Path(prepared.local_path)

        repo_root = Path(prepared.local_path)
        facts = scan_repo(repo_root, max_file_read_bytes)
        result = apply_filter(facts, max_file_read_bytes)

        result_dict = asdict(result)
        result_dict["repo_id"] = repo_id
        result_dict["repo_url"] = repo_url
        result_dict["source"] = repo_url
        result_dict["local_path"] = prepared.local_path

        if debug:
            result_dict["debug"] = {
                "repo_url": repo_url,
                "clone_dir": prepared.local_path,
                "root_files": sorted(list(facts.root_files))[:50],
                "root_dirs": sorted(list(facts.root_dirs))[:50],
            }

        return result_dict

    except Exception as e:
        return {
            "repo_id": repo_id,
            "repo_url": repo_url,
            "source": repo_url,
            "local_path": prepared.local_path if prepared and prepared.local_path else "",
            "included": False,
            "inclusion_signals": [],
            "executability_signals": [],
            "exclusion_signals": ["WORKER_EXCEPTION"],
            "final_reason": f"excluded: worker exception: {type(e).__name__}: {e}",
            "details": {},
        }

    finally:
        if cleanup_clones and cloned_path and cloned_path.exists():
            try:
                shutil.rmtree(cloned_path)
            except Exception:
                pass


def flush_done_futures(
    pending_futures,
    all_results_fh,
    included_urls_fh,
    stats: Dict[str, int],
):
    if not pending_futures:
        return pending_futures

    done, not_done = wait(pending_futures, return_when=FIRST_COMPLETED)

    for fut in done:
        result = fut.result()
        all_results_fh.write(json.dumps(result, ensure_ascii=False) + "\n")
        all_results_fh.flush()

        stats["processed_unique_repos"] += 1

        if result.get("included"):
            included_urls_fh.write(result["repo_url"] + "\n")
            included_urls_fh.flush()
            stats["included"] += 1
        else:
            stats["excluded"] += 1

    return list(not_done)


# =========================
# Main
# =========================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream HF dataset, dedupe consecutive repo_id values, convert to GitHub URL, clone repo, run static website filter, and write included repo URLs."
    )
    parser.add_argument("--dataset", required=True, help="HF dataset name, e.g. nick007x/github-code-2025")
    parser.add_argument("--split", default="train", help="HF split name")
    parser.add_argument("--workdir", required=True, help="Working directory for cloned repositories")
    parser.add_argument("--outdir", required=True, help="Directory for outputs")
    parser.add_argument("--max-file-read-bytes", type=int, default=200_000)
    parser.add_argument("--cleanup-clones", action="store_true")
    parser.add_argument("--max-repos", type=int, default=None, help="Process only first N unique repos for testing")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-pending", type=int, default=16)
    parser.add_argument("--hf-token", default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    all_results_jsonl = outdir / "all_repo_filter_results.jsonl"
    included_urls_txt = outdir / "included_repo_urls.txt"
    summary_json = outdir / "summary.json"

    stats = {
        "submitted_unique_repos": 0,
        "processed_unique_repos": 0,
        "included": 0,
        "excluded": 0,
    }

    pending_futures: List[Any] = []

    with (
        all_results_jsonl.open("w", encoding="utf-8") as all_results_fh,
        included_urls_txt.open("w", encoding="utf-8") as included_urls_fh,
        ProcessPoolExecutor(
            max_workers=args.workers,
            mp_context=mp.get_context("spawn"),
        ) as executor,
    ):
        for repo_id in stream_unique_repo_ids(args.dataset, args.split, args.hf_token):
            if args.max_repos is not None and stats["submitted_unique_repos"] >= args.max_repos:
                break

            future = executor.submit(
                process_repo_id,
                repo_id,
                str(workdir),
                args.max_file_read_bytes,
                args.cleanup_clones,
                args.debug,
            )
            pending_futures.append(future)
            stats["submitted_unique_repos"] += 1

            if len(pending_futures) >= args.max_pending:
                pending_futures = flush_done_futures(
                    pending_futures,
                    all_results_fh,
                    included_urls_fh,
                    stats,
                )

        while pending_futures:
            pending_futures = flush_done_futures(
                pending_futures,
                all_results_fh,
                included_urls_fh,
                stats,
            )

    summary = {
        "dataset": args.dataset,
        "split": args.split,
        "workdir": str(workdir),
        "outdir": str(outdir),
        "max_file_read_bytes": args.max_file_read_bytes,
        "cleanup_clones": args.cleanup_clones,
        "max_repos": args.max_repos,
        "workers": args.workers,
        "max_pending": args.max_pending,
        "debug": args.debug,
        **stats,
        "outputs": {
            "all_repo_filter_results_jsonl": str(all_results_jsonl),
            "included_repo_urls_txt": str(included_urls_txt),
            "summary_json": str(summary_json),
        },
    }

    with summary_json.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"Submitted unique repos: {stats['submitted_unique_repos']}")
    print(f"Processed unique repos: {stats['processed_unique_repos']}")
    print(f"Included: {stats['included']}")
    print(f"Excluded: {stats['excluded']}")
    print("Outputs written:")
    print(f"  - {all_results_jsonl}")
    print(f"  - {included_urls_txt}")
    print(f"  - {summary_json}")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
