"""Smoke one official cheap model call from inside Compose."""

from __future__ import annotations

import asyncio

from backend.llm.calls import qwen_json_async


def main() -> int:
    raw = asyncio.run(qwen_json_async(
        system="Reply with a tiny JSON object.",
        messages=[{"role": "user", "content": "return {\"ok\": true}"}],
        purpose="ping",
    ))
    print(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
