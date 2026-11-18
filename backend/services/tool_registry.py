"""Registered tools for a session. Production chat runs them via planned child runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .run_names import StepType, step_name


@dataclass(frozen=True)
class Tool:
    """One registered tool. `schema` is what the model sees.

    Handlers and `task_run` read `side_effect`, `idempotency_required`,
    `timeout_seconds`, and `retryable_errors` when executing a call.
    """

    name: str
    description: str
    fn: Callable[..., dict[str, Any]]
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    requires_approval: bool = False
    side_effect: bool = False
    idempotency_required: bool = False
    timeout_seconds: int = 30
    retryable_errors: frozenset[str] = field(default_factory=frozenset)
    display: str = ""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            step_name(StepType.TOOL, tool.name)
            if tool.name in self._tools:
                raise ValueError(f"Tool {tool.name} already registered")
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self.find(name)
        if tool is None:
            raise ValueError(f"Tool {name} not found")
        return tool

    def find(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]