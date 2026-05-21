"""
Generate K instruction variant candidates for Phase 1 and Phase 2 using
Qwen Coder on the vLLM endpoint (OpenAI-compatible API).

Quality gate: each variant must mention at least one CWV-specific mechanism.
Near-duplicate filtering: Jaccard token similarity > threshold → discard.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from openai import OpenAI

from harness.prompt_optimisation.prompts.schema import DemoExample, InstructionCandidate
from harness.prompt_optimisation.prompts.templates import BASELINE_PHASE1, BASELINE_PHASE2

_DEDUP_THRESHOLD = 0.85
_K = 15

# At least one of these must appear in each candidate (case-insensitive)
_CWV_KEYWORDS = [
    "lcp", "inp", "cls", "largest contentful paint", "interaction to next paint",
    "cumulative layout shift", "lazy", "preload", "defer", "async",
    "image", "font", "cache", "render-blocking", "critical", "viewport",
    "width", "height", "layout shift", "resource hint",
]

_PHASE1_TIPS = [
    "Sort lcp_entries by renderTime descending before proposing fixes — target the slowest element first.",
    "Identify the single highest-impact element rather than listing everything.",
    "Be specific about file paths and exact code patterns to change.",
    "For CLS, trace cls_shifts sources to specific DOM elements — name the tagName and className.",
    "For INP, check inp_interactions for long processingStart delays and target those handlers.",
    "Mention lazy loading, preload hints, or image format conversions explicitly when applicable.",
    "For CLS, always suggest explicit width/height attributes on images and iframes.",
    "Prioritise changes that affect the critical rendering path over cosmetic improvements.",
    "Reference the actual metric values (e.g., 'current LCP is 3800ms, target <2500ms').",
    "Suggest render-blocking resource removal or async/defer for non-critical scripts.",
]

_PHASE2_TIPS = [
    "Implement each plan item atomically — one file change per fix.",
    "Add width and height attributes to every img tag found.",
    "Add loading='lazy' to all below-the-fold images.",
    "Add <link rel='preload'> for the LCP element's resource in the <head>.",
    "Add async or defer to non-critical script tags.",
    "Use font-display: swap for all @font-face declarations.",
    "Remove or async-load any render-blocking stylesheets.",
    "Do not change content, navigation, or visible layout — only performance attributes.",
    "Check the patch compiles/builds by reading the relevant config files first.",
]


def _make_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
        base_url=os.environ.get("OPENCODE_OPENAI_BASE_URL", "http://localhost:8000/v1"),
    )


def _model_name() -> str:
    return os.environ.get("VLLM_SERVED_MODEL_NAME", "qwen3-coder-next")


def _jaccard(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def _has_cwv_keyword(text: str) -> bool:
    tl = text.lower()
    return any(kw in tl for kw in _CWV_KEYWORDS)


def _deduplicate(candidates: list[str], threshold: float = _DEDUP_THRESHOLD) -> list[str]:
    kept: list[str] = []
    for c in candidates:
        if all(_jaccard(c, k) < threshold for k in kept):
            kept.append(c)
    return kept


def _propose(
    client: OpenAI,
    model: str,
    phase: str,
    baseline: str,
    demo_examples: list[DemoExample],
    tips: list[str],
    k: int,
    temperature: float,
) -> list[str]:
    demo_block = ""
    if demo_examples:
        demo_block = "\n\n## Examples of successful outcomes\n\n"
        demo_block += "\n\n".join(d.as_markdown() for d in demo_examples[:3])

    tips_block = "\n".join(f"- {t}" for t in tips)

    meta_prompt = f"""You are an expert at writing LLM prompts for web performance optimization tasks.

Your job: generate {k} distinct instruction variants for {phase} of a two-phase Core Web Vitals (CWV) optimization agent.

## Current baseline instruction (to vary from):
```
{baseline}
```
{demo_block}

## Rewriting tips — incorporate these ideas into your variants:
{tips_block}

## Rules:
1. Each variant must be a complete, self-contained instruction (not a diff or partial edit).
2. Each variant must preserve the output format instructions (write plan.md / apply patch).
3. Each variant must explicitly mention at least one specific CWV mechanism (LCP, INP, CLS, lazy-load, preload, etc.).
4. Variants must be meaningfully different from each other — not just synonym swaps.
5. Keep ${"{FRAMEWORK}"}, ${"{CWV_MOBILE}"}, ${"{CWV_DESKTOP}"} placeholders exactly as-is where present.

## Output format:
Return a JSON array of {k} strings, each being one complete instruction variant.
No explanation, no markdown wrapper — just the raw JSON array.
"""

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": meta_prompt}],
        temperature=temperature,
        max_tokens=8000,
    )
    raw = response.choices[0].message.content or "[]"

    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())

    try:
        variants = json.loads(raw)
        if not isinstance(variants, list):
            raise ValueError("not a list")
        return [str(v).strip() for v in variants if v]
    except Exception:
        # Fallback: extract quoted blocks
        return re.findall(r'"((?:[^"\\]|\\.){50,})"', raw)


def generate_candidates(
    demo_examples: list[DemoExample],
    run_dir: Path,
    k: int = _K,
    temperature: float = 0.9,
) -> tuple[list[InstructionCandidate], list[InstructionCandidate]]:
    """
    Generate k phase1 + k phase2 instruction candidates.

    Returns:
        (phase1_candidates, phase2_candidates)
    """
    client = _make_client()
    model = _model_name()

    print(f"[proposer] Generating {k} phase1 candidates via {model} …")
    raw_p1 = _propose(client, model, "Phase 1 (planning)", BASELINE_PHASE1,
                      demo_examples, _PHASE1_TIPS, k, temperature)

    print(f"[proposer] Generating {k} phase2 candidates via {model} …")
    raw_p2 = _propose(client, model, "Phase 2 (execution)", BASELINE_PHASE2,
                      demo_examples, _PHASE2_TIPS, k, temperature)

    # Quality gate: must contain CWV keyword
    raw_p1 = [c for c in raw_p1 if _has_cwv_keyword(c)]
    raw_p2 = [c for c in raw_p2 if _has_cwv_keyword(c)]

    # Deduplication
    raw_p1 = _deduplicate(raw_p1)
    raw_p2 = _deduplicate(raw_p2)

    # Always prepend the baseline as candidate 0 so Optuna can select it
    if BASELINE_PHASE1 not in raw_p1:
        raw_p1 = [BASELINE_PHASE1] + raw_p1
    if BASELINE_PHASE2 not in raw_p2:
        raw_p2 = [BASELINE_PHASE2] + raw_p2

    p1_candidates = [
        InstructionCandidate(phase="phase1", text=t, candidate_idx=i,
                             source="baseline" if i == 0 else "proposed")
        for i, t in enumerate(raw_p1)
    ]
    p2_candidates = [
        InstructionCandidate(phase="phase2", text=t, candidate_idx=i,
                             source="baseline" if i == 0 else "proposed")
        for i, t in enumerate(raw_p2)
    ]

    # Persist to disk
    candidates_path = run_dir / "candidates.jsonl"
    with candidates_path.open("w") as f:
        for c in p1_candidates + p2_candidates:
            f.write(c.model_dump_json() + "\n")

    print(f"[proposer] {len(p1_candidates)} phase1, {len(p2_candidates)} phase2 → {candidates_path}")
    return p1_candidates, p2_candidates


def load_candidates(
    run_dir: Path,
) -> tuple[list[InstructionCandidate], list[InstructionCandidate]]:
    """Reload candidates saved by generate_candidates."""
    p1, p2 = [], []
    for line in (run_dir / "candidates.jsonl").read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        c = InstructionCandidate.model_validate_json(line)
        (p1 if c.phase == "phase1" else p2).append(c)
    return p1, p2
