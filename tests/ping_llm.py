"""Live ping of the campus catalog. Needs the school key and campus net or VPN.

    ./dev py tests/ping_llm.py
    ./dev py tests/ping_llm.py --model qwen
"""

from __future__ import annotations

import argparse
import sys

from backend.llm.client import campus_client
from backend.llm_config import LLM_CONFIGS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="one campus model id, or omit to ping every catalog row")
    args = ap.parse_args()
    client = campus_client()
    ids = [args.model] if args.model else [row.model for row in LLM_CONFIGS]
    failed = 0
    for model_id in ids:
        print(f"\n=== {model_id} ===")
        try:
            resp = client.chat_completion(
                model_id,
                [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Only reply with 'pong'."},
                ],
                temperature=0,
            )
            text = str(resp["choices"][0]["message"]["content"] or "")
            print("reply:", repr(text))
        except Exception as e:
            failed += 1
            print(f"FAIL: {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
