"""CWV Optimizer - Production-grade Core Web Vitals optimization pipeline.

A LangGraph-based workflow for optimizing Core Web Vitals performance
through AI-driven code analysis and modifications.
"""

__version__ = "0.1.0"
__author__ = "CWV Team"

from cwv_optimizer.langgraph_app.graphs.main_graph import (
    create_full_pipeline_graph,
    create_optimization_graph,
)
from cwv_optimizer.langgraph_app.state.state_schema import OptimizationState

__all__ = [
    "create_full_pipeline_graph",
    "create_optimization_graph",
    "OptimizationState",
    "__version__",
]
