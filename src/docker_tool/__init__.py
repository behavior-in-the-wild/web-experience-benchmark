"""Shared Docker-backed hosting and sandbox utilities."""

from docker_tool.hosting import HostResult, start_host, stop_host

__all__ = ["HostResult", "start_host", "stop_host"]
