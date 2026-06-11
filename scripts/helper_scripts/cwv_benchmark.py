#!/usr/bin/env python3
"""Compatibility launcher for the canonical CWV benchmark tool."""

import runpy
import sys
from pathlib import Path


def main() -> None:
    tool_path = Path(__file__).resolve().parents[2] / "src" / "cwv_tool" / "cwv_benchmark.py"
    sys.argv[0] = str(tool_path)
    runpy.run_path(str(tool_path), run_name="__main__")


if __name__ == "__main__":
    main()
