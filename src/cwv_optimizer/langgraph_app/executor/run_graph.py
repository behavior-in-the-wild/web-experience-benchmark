"""Entrypoint to run LangGraph workflows."""

from __future__ import annotations

from typing import Any, Dict

from cwv_optimizer.config import get_settings
from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.graphs.main_graph import (
    create_full_pipeline_graph,
    create_framework_pipeline_graph,
    create_graph_with_checkpointer,
    create_optimization_graph,
)

logger = get_logger(__name__)


async def run_full_pipeline(
    config: Dict[str, Any],
    *,
    use_checkpointing: bool = False,
    checkpoint_db: str | None = None,
) -> Dict[str, Any]:
    """Run the full pipeline: clone -> deploy -> analyze -> optimize -> test.

    Args:
        config: Pipeline configuration
        use_checkpointing: Enable SQLite checkpointing
        checkpoint_db: Path to checkpoint database

    Returns:
        Final state dictionary
    """
    settings = get_settings()
    db_path = checkpoint_db or settings.checkpoint_db

    graph = (
        create_graph_with_checkpointer(db_path, full_pipeline=True)
        if use_checkpointing
        else create_full_pipeline_graph()
    )

    logger.info("Starting full pipeline for: %s", config.get("github_url"))

    if use_checkpointing:
        config_id = {"configurable": {"thread_id": config.get("github_url", "default")}}
        return await graph.ainvoke(config, config=config_id)

    return await graph.ainvoke(config)


async def run_framework_pipeline(
    config: Dict[str, Any],
    *,
    use_checkpointing: bool = False,
    checkpoint_db: str | None = None,
) -> Dict[str, Any]:
    """Run the framework-based pipeline: clone -> deploy (framework) -> analyze -> optimize.

    Uses pre-detected framework info instead of AI to deploy the repository.

    Args:
        config: Pipeline configuration (must include 'github_url' and 'framework')
        use_checkpointing: Enable SQLite checkpointing
        checkpoint_db: Path to checkpoint database

    Returns:
        Final state dictionary
    """
    settings = get_settings()
    db_path = checkpoint_db or settings.checkpoint_db

    graph = (
        create_graph_with_checkpointer(db_path, framework_pipeline=True)
        if use_checkpointing
        else create_framework_pipeline_graph()
    )

    logger.info(
        "Starting framework pipeline for: %s (framework: %s)",
        config.get("github_url"),
        config.get("framework"),
    )

    if use_checkpointing:
        config_id = {"configurable": {"thread_id": config.get("github_url", "default")}}
        return await graph.ainvoke(config, config=config_id)

    return await graph.ainvoke(config)


async def run_optimization_workflow(
    config: Dict[str, Any],
    *,
    use_checkpointing: bool = False,
    checkpoint_db: str | None = None,
) -> Dict[str, Any]:
    """Run the optimization-only workflow (requires existing workspace + suggestions).

    Args:
        config: Pipeline configuration
        use_checkpointing: Enable SQLite checkpointing
        checkpoint_db: Path to checkpoint database

    Returns:
        Final state dictionary
    """
    settings = get_settings()
    db_path = checkpoint_db or settings.checkpoint_db

    graph = (
        create_graph_with_checkpointer(db_path, full_pipeline=False)
        if use_checkpointing
        else create_optimization_graph()
    )

    logger.info("Starting optimization workflow for: %s", config.get("url"))

    if use_checkpointing:
        config_id = {"configurable": {"thread_id": config.get("url", "default")}}
        return await graph.ainvoke(config, config=config_id)

    return await graph.ainvoke(config)


async def run_workflow_stream(
    config: Dict[str, Any],
    *,
    full_pipeline: bool = False,
    framework_pipeline: bool = False,
    use_checkpointing: bool = False,
    checkpoint_db: str | None = None,
) -> Dict[str, Any]:
    """Run workflow with streaming output.

    Args:
        config: Pipeline configuration
        full_pipeline: Use full pipeline vs optimization only
        framework_pipeline: Use framework-based pipeline
        use_checkpointing: Enable SQLite checkpointing
        checkpoint_db: Path to checkpoint database

    Returns:
        Final state dictionary
    """
    settings = get_settings()
    db_path = checkpoint_db or settings.checkpoint_db

    if framework_pipeline:
        graph = (
            create_graph_with_checkpointer(db_path, framework_pipeline=True)
            if use_checkpointing
            else create_framework_pipeline_graph()
        )
        thread_id = config.get("github_url", "default")
    elif full_pipeline:
        graph = (
            create_graph_with_checkpointer(db_path, full_pipeline=True)
            if use_checkpointing
            else create_full_pipeline_graph()
        )
        thread_id = config.get("github_url", "default")
    else:
        graph = (
            create_graph_with_checkpointer(db_path, full_pipeline=False)
            if use_checkpointing
            else create_optimization_graph()
        )
        thread_id = config.get("url", "default")

    logger.info("Starting streaming workflow")

    if use_checkpointing:
        stream = graph.astream(
            config,
            config={"configurable": {"thread_id": thread_id}},
        )
    else:
        stream = graph.astream(config)

    last_event: Dict[str, Any] = {}
    async for event in stream:
        node_name = next(iter(event))
        logger.info("Node completed: %s", node_name)
        last_event = event

    return last_event

