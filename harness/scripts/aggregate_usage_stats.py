#!/usr/bin/env python3
"""
Filter *_usage.json rows and print aggregate stats.

Strict (default) — all listed fields must be > 0 for that agent family:

  codex:    input > 0 AND cached_input > 0 AND output > 0
  opencode: input > 0 AND cache_read > 0 AND (output + reasoning) > 0
  aider:    input > 0 AND output > 0
  claude:   input > 0 AND output > 0 AND cache_read > 0 AND cache_creation > 0

Lenient (--lenient): keep a file if the previous “sum of token counters > 0” rule passes.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any


def _tokens(d: dict[str, Any]) -> dict[str, Any]:
    t = d.get("tokens")
    return t if isinstance(t, dict) else {}


def lenient_ok(agent: str, t: dict[str, Any]) -> bool:
    if agent == "codex":
        s = (t.get("input") or 0) + (t.get("cached_input") or 0) + (t.get("output") or 0)
    elif agent == "opencode":
        s = (
            (t.get("input") or 0)
            + (t.get("output") or 0)
            + (t.get("reasoning") or 0)
            + (t.get("cache_read") or 0)
            + (t.get("cache_write") or 0)
        )
    elif agent == "aider":
        s = (t.get("input") or 0) + (t.get("output") or 0)
    elif agent == "claude":
        s = (
            (t.get("input") or 0)
            + (t.get("output") or 0)
            + (t.get("cache_read") or 0)
            + (t.get("cache_creation") or 0)
        )
    else:
        return False
    return s > 0


def strict_ok(agent: str, t: dict[str, Any]) -> bool:
    if agent == "codex":
        return (
            (t.get("input") or 0) > 0
            and (t.get("cached_input") or 0) > 0
            and (t.get("output") or 0) > 0
        )
    if agent == "opencode":
        return (
            (t.get("input") or 0) > 0
            and (t.get("cache_read") or 0) > 0
            and ((t.get("output") or 0) + (t.get("reasoning") or 0)) > 0
        )
    if agent == "aider":
        return (t.get("input") or 0) > 0 and (t.get("output") or 0) > 0
    if agent == "claude":
        return (
            (t.get("input") or 0) > 0
            and (t.get("output") or 0) > 0
            and (t.get("cache_read") or 0) > 0
            and (t.get("cache_creation") or 0) > 0
        )
    return False


def detect_agent(path: str) -> str:
    b = os.path.basename(path)
    if "_template_codex_" in b:
        return "codex"
    if "_template_opencodegpt41_" in b:
        return "opencode"
    if "_template_opencodegpt51codex_" in b:
        return "opencode"
    if "_template_opencode_" in b:
        return "opencode"
    if "_template_aider_" in b:
        return "aider"
    if "_template_claudecode_" in b:
        return "claude"
    return "unknown"


def load_rows(pattern: str, strict: bool) -> tuple[list[dict[str, Any]], list[str]]:
    pred = strict_ok if strict else lenient_ok
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    for f in sorted(glob.glob(pattern)):
        agent = detect_agent(f)
        if agent == "unknown":
            skipped.append(f"{os.path.basename(f)} (unknown agent)")
            continue
        d = json.load(open(f, encoding="utf-8"))
        t = _tokens(d)
        if not pred(agent, t):
            skipped.append(os.path.basename(f))
            continue
        kept.append(d)
    return kept, skipped


def summarize(label: str, rows: list[dict[str, Any]]) -> None:
    n = len(rows)
    if n == 0:
        print(f"{label}: no rows after filter")
        return
    walls = [float(d.get("wall_clock_seconds") or 0) for d in rows]
    tools = [float(d.get("tool_calls") or 0) for d in rows]
    costs = [float(d["cost_usd"]) for d in rows if d.get("cost_usd") is not None]
    print(f"{label}")
    print(f"  n={n}")
    print(f"  avg wall_clock_seconds: {sum(walls)/n:.1f}")
    print(f"  avg tool_calls:         {sum(tools)/n:.1f}")
    if costs:
        print(f"  avg cost_usd (present): {sum(costs)/len(costs):.4f}  (over {len(costs)} files with cost)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pattern", help="Glob of *_usage.json files, e.g. out/RUN/results/*_usage.json")
    p.add_argument("--lenient", action="store_true", help="Use sum>0 rule instead of strict per-field rules")
    p.add_argument("--label", default="", help="Label for output")
    args = p.parse_args()

    rows, skipped = load_rows(args.pattern, strict=not args.lenient)
    label = args.label or args.pattern
    summarize(label, rows)
    mode = "lenient" if args.lenient else "strict"
    print(f"  filter={mode}  skipped={len(skipped)}")
    if skipped:
        ids: set[str] = set()
        for x in skipped:
            stem = os.path.basename(x.split(" ", 1)[0])
            head = stem.split("_", 1)[0]
            if head.isdigit():
                ids.add(head)
        if ids:
            print(f"  skipped_ids: {', '.join(sorted(ids))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
