from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, field_validator


class DemoExample(BaseModel):
    repo_id: str
    framework: str
    baseline_summary: str   # condensed JSON, fits in ~200 tokens
    plan_excerpt: str        # first 60 lines of plan.md
    patch_excerpt: str       # first 80 lines of git diff
    lcp_delta_pct: float
    inp_delta_pct: float
    cls_delta_pct: float

    def as_markdown(self) -> str:
        return (
            f"### Example: {self.repo_id} ({self.framework})\n"
            f"**Baseline CWV:** {self.baseline_summary}\n"
            f"**Result:** LCP {self.lcp_delta_pct:+.1f}%  "
            f"INP {self.inp_delta_pct:+.1f}%  CLS {self.cls_delta_pct:+.1f}%\n\n"
            f"**Plan excerpt:**\n```\n{self.plan_excerpt}\n```\n\n"
            f"**Patch excerpt:**\n```diff\n{self.patch_excerpt}\n```\n"
        )


class InstructionCandidate(BaseModel):
    phase: Literal["phase1", "phase2"]
    text: str                 # full instruction with ${FRAMEWORK} etc. placeholders
    candidate_idx: int
    source: Literal["baseline", "proposed"]


class PromptConfig(BaseModel):
    phase1_instruction: str   # instruction text, demos embedded as ## Examples section
    phase2_instruction: str
    demos: list[DemoExample]
    config_hash: str = ""

    @field_validator("config_hash", mode="before")
    @classmethod
    def _set_hash(cls, v: str, info) -> str:  # noqa: N805
        if v:
            return v
        data = info.data
        raw = (data.get("phase1_instruction", "") + data.get("phase2_instruction", "")).encode()
        return hashlib.sha256(raw).hexdigest()[:16]

    def model_post_init(self, __context) -> None:  # noqa: ANN001
        if not self.config_hash:
            raw = (self.phase1_instruction + self.phase2_instruction).encode()
            object.__setattr__(self, "config_hash", hashlib.sha256(raw).hexdigest()[:16])

    @classmethod
    def build(
        cls,
        phase1_text: str,
        phase2_text: str,
        demos: list[DemoExample],
    ) -> "PromptConfig":
        """Embed demos as a markdown section appended to the phase1 instruction."""
        p1 = phase1_text
        if demos:
            examples_block = "\n\n## Examples of successful optimizations\n\n"
            examples_block += "\n\n".join(d.as_markdown() for d in demos)
            p1 = phase1_text + examples_block
        return cls(phase1_instruction=p1, phase2_instruction=phase2_text, demos=demos)
