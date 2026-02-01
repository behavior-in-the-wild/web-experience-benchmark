"""Main LangGraph graph assembly for CWV optimization pipeline."""

from __future__ import annotations

from typing import Optional

from langgraph.graph import END, StateGraph

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
except ImportError:
    AsyncSqliteSaver = None

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes import (
    post_processing_node,
    apply_code_optimizations_node,
    clone_repo_node,
    cwv_analysis_node,
    repo_analyzer_node,
    deploy_generator_node,
    framework_deploy_node,
    run_performance_testing_node,
    validate_full_pipeline_node,
    validate_framework_pipeline_node,
    validate_input_node,
    visual_regression_node,
)

logger = get_logger(__name__)


def create_full_pipeline_graph(checkpointer=None):
    """Build and compile the full optimization workflow graph.

    This is the main pipeline that:
    1. Validates the GitHub URL input
    2. Clones a GitHub repository
    3. Analyzes repository structure (repo_analyzer)
    4. Generates deployment script and starts server (deploy_generator)
    5. Runs CWV analysis to generate suggestions
    6. Applies code optimizations
    7. Tests for visual regression
    8. Runs performance testing
    9. Archives results

    Args:
        checkpointer: Optional checkpointer for state persistence

    Returns:
        Compiled LangGraph application
    """
    graph = StateGraph(dict)

    # Add all nodes
    graph.add_node("validate", validate_full_pipeline_node)
    graph.add_node("clone_repo", clone_repo_node)
    graph.add_node("repo_analyzer", repo_analyzer_node)
    graph.add_node("deploy_generator", deploy_generator_node)
    graph.add_node("cwv_analysis", cwv_analysis_node)
    graph.add_node("apply_code_optimizations", apply_code_optimizations_node)
    graph.add_node("visual_regression", visual_regression_node)
    graph.add_node("run_performance_testing", run_performance_testing_node)
    graph.add_node("post_processing", post_processing_node)

    # Define the flow
    graph.set_entry_point("validate")
    graph.add_edge("validate", "clone_repo")
    graph.add_edge("clone_repo", "repo_analyzer")
    graph.add_edge("repo_analyzer", "deploy_generator")
    graph.add_edge("deploy_generator", "cwv_analysis")
    graph.add_edge("cwv_analysis", "apply_code_optimizations")
    graph.add_edge("apply_code_optimizations", "visual_regression")
    graph.add_edge("visual_regression", "run_performance_testing")
    graph.add_edge("run_performance_testing", "post_processing")
    graph.add_edge("post_processing", END)

    logger.debug("Full pipeline graph created with 9 nodes")

    if checkpointer:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()


def create_framework_pipeline_graph(checkpointer=None):
    """Build and compile the framework-based deployment workflow graph.

    This pipeline uses pre-detected framework information instead of AI analysis:
    1. Validates the GitHub URL and framework input
    2. Clones the repository
    3. Deploys using framework-specific commands (skips AI analysis)
    4. Runs CWV analysis to generate suggestions
    5. Applies code optimizations
    6. Tests for visual regression
    7. Runs performance testing
    8. Archives results

    Input state should include:
        - github_url: URL of the repository
        - framework: One of "Hexo", "Jekyll", "Static HTML"

    Args:
        checkpointer: Optional checkpointer for state persistence

    Returns:
        Compiled LangGraph application
    """
    graph = StateGraph(dict)

    # Add all nodes (note: uses framework_deploy instead of repo_analyzer + deploy_generator)
    graph.add_node("validate", validate_framework_pipeline_node)
    graph.add_node("clone_repo", clone_repo_node)
    graph.add_node("framework_deploy", framework_deploy_node)
    graph.add_node("cwv_analysis", cwv_analysis_node)
    graph.add_node("apply_code_optimizations", apply_code_optimizations_node)
    graph.add_node("visual_regression", visual_regression_node)
    graph.add_node("run_performance_testing", run_performance_testing_node)
    graph.add_node("post_processing", post_processing_node)

    # Define the flow (skips repo_analyzer, goes directly to framework_deploy)
    graph.set_entry_point("validate")
    graph.add_edge("validate", "clone_repo")
    graph.add_edge("clone_repo", "framework_deploy")
    graph.add_edge("framework_deploy", "cwv_analysis")
    graph.add_edge("cwv_analysis", "apply_code_optimizations")
    graph.add_edge("apply_code_optimizations", "visual_regression")
    graph.add_edge("visual_regression", "run_performance_testing")
    graph.add_edge("run_performance_testing", "post_processing")
    graph.add_edge("post_processing", END)

    logger.debug("Framework pipeline graph created with 8 nodes")

    if checkpointer:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()



def create_optimization_graph(checkpointer=None):
    """Build and compile the optimization workflow graph.

    This is the optimization-only pipeline that starts with an already-deployed
    URL and parsed suggestions (skips clone, deploy, cwv_analysis steps).

    Args:
        checkpointer: Optional checkpointer for state persistence

    Returns:
        Compiled LangGraph application
    """
    graph = StateGraph(dict)

    # Add validation first, then optimization nodes
    graph.add_node("validate", validate_input_node)
    graph.add_node("apply_code_optimizations", apply_code_optimizations_node)
    graph.add_node("visual_regression", visual_regression_node)
    graph.add_node("run_performance_testing", run_performance_testing_node)
    graph.add_node("post_processing", post_processing_node)

    graph.set_entry_point("validate")
    graph.add_edge("validate", "apply_code_optimizations")
    graph.add_edge("apply_code_optimizations", "visual_regression")
    graph.add_edge("visual_regression", "run_performance_testing")
    graph.add_edge("run_performance_testing", "post_processing")
    graph.add_edge("post_processing", END)

    logger.debug("Optimization graph created with 5 nodes")

    if checkpointer:
        return graph.compile(checkpointer=checkpointer)
    return graph.compile()


def create_graph_with_checkpointer(
    db_path: str = "checkpoints.db",
    full_pipeline: bool = False,
    framework_pipeline: bool = False,
):
    """Create a graph configured with a SQLite-based checkpointer.

    Args:
        db_path: Path to the SQLite database for checkpointing
        full_pipeline: If True, use the full pipeline (clone -> deploy -> analyze)
                      If False, use the optimization-only pipeline
        framework_pipeline: If True, use the framework-based pipeline

    Returns:
        Compiled LangGraph application with checkpointing
    """
    # Note: For async execution, use AsyncSqliteSaver in an async context
    # For sync execution, use MemorySaver as a simpler alternative
    from langgraph.checkpoint.memory import MemorySaver
    
    checkpointer = MemorySaver()

    if framework_pipeline:
        return create_framework_pipeline_graph(checkpointer=checkpointer)
    if full_pipeline:
        return create_full_pipeline_graph(checkpointer=checkpointer)
    return create_optimization_graph(checkpointer=checkpointer)

