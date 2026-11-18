"""Tool registration shim. Use ``backend.services.tool_registry`` directly."""

from __future__ import annotations

from .tool_registry import Tool, ToolRegistry

__all__ = ["Tool", "ToolRegistry"]