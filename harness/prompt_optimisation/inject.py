"""
Permanently patch harness/agents/template_opencode_os.sh with the winning
instruction text from a completed optimization run.

Gated by allow_permanent_inject: false in config.yaml — requires explicit
--force flag or config change before any template modification occurs.

Creates a .bak backup before modifying.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
TEMPLATE = ROOT / "harness" / "agents" / "template_opencode_os.sh"
CONFIG = Path(__file__).parent / "config.yaml"

# Markers that bracket the default heredoc / printf blocks in the template.
# The override hook already wraps them in `else ... fi`, so we replace the
# content between `else` and `fi` (exclusive).
_PHASE1_ELSE_MARKER = "else\ncat <<EOF > \"$PLAN_PROMPT\""
_PHASE1_FI_MARKER = "EOF\nfi\n\ncp \"$PLAN_PROMPT\""

_PHASE2_ELSE_MARKER = "else\n{"
_PHASE2_FI_MARKER = "} > \"$EXEC_PROMPT\"\nfi"


def _read_config() -> dict:
    if CONFIG.exists():
        return yaml.safe_load(CONFIG.read_text()) or {}
    return {}


def inject(run_id: str, force: bool = False) -> None:
    """
    Patch the template with winning prompts from runs/{run_id}/best_prompt.json.

    Args:
        run_id: timestamp string, e.g. "20260521_140000"
        force:  bypass allow_permanent_inject guard
    """
    cfg = _read_config()
    allow = cfg.get("harness", {}).get("allow_permanent_inject", False)

    if not allow and not force:
        raise PermissionError(
            "allow_permanent_inject is false in config.yaml. "
            "Set it to true or pass --force."
        )

    run_dir = Path(__file__).parent / "runs" / run_id
    best_path = run_dir / "best_prompt.json"
    if not best_path.exists():
        raise FileNotFoundError(f"best_prompt.json not found in {run_dir}")

    best = json.loads(best_path.read_text())
    p1_text: str = best["phase1_instruction"]
    p2_text: str = best["phase2_instruction"]

    template_text = TEMPLATE.read_text()

    # ── Backup ────────────────────────────────────────────────────────────────
    bak = TEMPLATE.with_suffix(".sh.bak")
    shutil.copy2(TEMPLATE, bak)
    print(f"Backup: {bak}")

    # ── Replace Phase 1 default block ─────────────────────────────────────────
    # Find the region between the `else` marker and `fi` closing the Phase 1 block.
    # Pattern: the else branch of the PHASE1_INSTRUCTION if/else in the template.
    p1_pattern = re.compile(
        r"(else\n)cat <<EOF > \"\$PLAN_PROMPT\"\n(.*?)EOF(\nfi\n\ncp \"\$PLAN_PROMPT\")",
        re.DOTALL,
    )
    new_p1_block = f"else\ncat <<'OPTEOF' > \"$PLAN_PROMPT\"\n{p1_text}\nOPTEOF\\3"
    # Use lambda to avoid backreference interpretation of p1_text
    new_template, n1 = p1_pattern.subn(
        lambda m: f"else\ncat <<'OPTEOF' > \"$PLAN_PROMPT\"\n{p1_text}\nOPTEOF\nfi\n\ncp \"$PLAN_PROMPT\"",
        template_text,
    )
    if n1 == 0:
        raise RuntimeError("Could not locate Phase 1 default block in template — pattern mismatch")

    # ── Replace Phase 2 default block ─────────────────────────────────────────
    p2_pattern = re.compile(
        r"(else\n)\{(.*?)\} > \"\$EXEC_PROMPT\"\n(fi)",
        re.DOTALL,
    )
    new_template, n2 = p2_pattern.subn(
        lambda m: f"else\n{{\n  printf '%s' \"{{}}\"\n}} > \"$EXEC_PROMPT\"\nfi",
        new_template,
    )
    # The above is wrong — we want to replace with a literal heredoc approach.
    # Re-do: simple heredoc replacement for phase2.
    new_template = re.sub(
        r"(else\n)\{.*?\} > \"\$EXEC_PROMPT\"\n(fi)",
        lambda m: (
            "else\n"
            "cat <<'OPTEOF2' > \"$EXEC_PROMPT\"\n"
            + p2_text + "\n"
            "OPTEOF2\n"
            "fi"
        ),
        new_template,
        flags=re.DOTALL,
    )

    TEMPLATE.write_text(new_template)

    # ── Record ────────────────────────────────────────────────────────────────
    record = {
        "injected_at": datetime.utcnow().isoformat(),
        "run_id": run_id,
        "train_score": best.get("train_score"),
        "validation_score": best.get("validation_score"),
        "config_hash": best.get("config_hash"),
    }
    (run_dir / "inject_record.json").write_text(json.dumps(record, indent=2))

    print(f"Patched {TEMPLATE}")
    print(f"Record: {run_dir / 'inject_record.json'}")
