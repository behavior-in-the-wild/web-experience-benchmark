from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FrameworkSpec:
    key: str
    image: str
    legacy_script: str
    aliases: tuple[str, ...]


IMAGE_PREFIX = "web-bench"

FRAMEWORKS: dict[str, FrameworkSpec] = {
    "static": FrameworkSpec(
        key="static",
        image=f"{IMAGE_PREFIX}/host-static:latest",
        legacy_script="host_static_html.sh",
        aliases=("static html", "statichtml", "static", "html"),
    ),
    "express": FrameworkSpec(
        key="express",
        image=f"{IMAGE_PREFIX}/host-node:latest",
        legacy_script="host_express.sh",
        aliases=("express",),
    ),
    "react": FrameworkSpec(
        key="react",
        image=f"{IMAGE_PREFIX}/host-node:latest",
        legacy_script="host_react.sh",
        aliases=("react",),
    ),
    "next": FrameworkSpec(
        key="next",
        image=f"{IMAGE_PREFIX}/host-node:latest",
        legacy_script="host_next.sh",
        aliases=("next", "next.js", "nextjs"),
    ),
    "vue": FrameworkSpec(
        key="vue",
        image=f"{IMAGE_PREFIX}/host-node:latest",
        legacy_script="host_vue.sh",
        aliases=("vue", "vue.js", "vuejs"),
    ),
    "hexo": FrameworkSpec(
        key="hexo",
        image=f"{IMAGE_PREFIX}/host-node:latest",
        legacy_script="host_hexo.sh",
        aliases=("hexo",),
    ),
    "flask": FrameworkSpec(
        key="flask",
        image=f"{IMAGE_PREFIX}/host-python:latest",
        legacy_script="host_flask.sh",
        aliases=("flask",),
    ),
    "pelican": FrameworkSpec(
        key="pelican",
        image=f"{IMAGE_PREFIX}/host-python:latest",
        legacy_script="host_pelican.sh",
        aliases=("pelican",),
    ),
    "jekyll": FrameworkSpec(
        key="jekyll",
        image=f"{IMAGE_PREFIX}/host-ruby:latest",
        legacy_script="host_jekyll.sh",
        aliases=("jekyll",),
    ),
    "hugo": FrameworkSpec(
        key="hugo",
        image=f"{IMAGE_PREFIX}/host-hugo:latest",
        legacy_script="host_hugo.sh",
        aliases=("hugo",),
    ),
    "quarto": FrameworkSpec(
        key="quarto",
        image=f"{IMAGE_PREFIX}/host-quarto:latest",
        legacy_script="host_quarto.sh",
        aliases=("quarto",),
    ),
}

_ALIASES = {alias: spec for spec in FRAMEWORKS.values() for alias in spec.aliases}


def normalize_framework(framework: str | None, host_file_path: str | None = None) -> FrameworkSpec:
    value = (framework or "").strip().lower()
    if value in _ALIASES:
        return _ALIASES[value]

    host_name = Path(host_file_path or "").name.lower()
    for spec in FRAMEWORKS.values():
        if host_name == spec.legacy_script.lower():
            return spec

    label = framework or host_file_path or "<empty>"
    raise ValueError(f"unknown framework/host file: {label}")


def all_images() -> list[str]:
    return sorted({spec.image for spec in FRAMEWORKS.values()})
