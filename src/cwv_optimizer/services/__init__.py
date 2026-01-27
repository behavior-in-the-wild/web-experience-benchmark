"""External services module.

This module contains wrappers for external services like LLM clients,
code optimization tools, visual regression testing, and archival.
"""

from cwv_optimizer.services.code_optimizer import apply_code_optimizations
from cwv_optimizer.services.visual_regression import run_visual_regression_tests
from cwv_optimizer.services.performance_testing import run_cwv_tests
from cwv_optimizer.services.reporting import (
    analyze_performance_improvements,
    generate_learnings,
)
from cwv_optimizer.services.archival import consolidate_and_archive_results, generate_patches

__all__ = [
    "apply_code_optimizations",
    "run_visual_regression_tests",
    "run_cwv_tests",
    "analyze_performance_improvements",
    "generate_learnings",
    "consolidate_and_archive_results",
    "generate_patches",
]
