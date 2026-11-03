"""Campus model ids the call sites pass by name.

The url and the key stay in the environment.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import env


@dataclass(frozen=True)
class LLMConfig:
    id: str
    base_url: str
    model: str
    thinking: bool = False


LLM_CONFIGS: tuple[LLMConfig, ...] = (
    LLMConfig(
        id="deepseek-chat",
        base_url=env("SJTU_BASE_URL"),
        model="deepseek-chat",
    ),
    LLMConfig(
        id="deepseek-reasoner",
        base_url=env("SJTU_BASE_URL"),
        model="deepseek-reasoner",
        thinking=True,
    ),
    LLMConfig(
        id="minimax",
        base_url=env("SJTU_BASE_URL"),
        model="minimax",
    ),
    LLMConfig(
        id="qwen",
        base_url=env("SJTU_BASE_URL"),
        model="qwen",
    ),
)
