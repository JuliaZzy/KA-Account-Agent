"""Official model ids shown in the desk dropdown.

Chat steps pick their own model; this list is display-only. Base URLs stay in the
environment and are not used by the chat path.
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
        id="qwen3.7-flash",
        base_url=env("QWEN_BASE_URL"),
        model="qwen3.7-flash",
    ),
    LLMConfig(
        id="deepseek-flash",
        base_url=env("DEEPSEEK_BASE_URL"),
        model="deepseek-flash",
    ),
)
