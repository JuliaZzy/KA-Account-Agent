"""Smoke one official cheap model call from inside Compose."""

from __future__ import annotations

from backend.llm.calls import school_json


def main() -> int:
    raw = school_json(
        system="Reply with a tiny JSON object.",
        messages=[{"role": "user", "content": "return {\"ok\": true}"}],
        purpose="ping",
    )
    print(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
