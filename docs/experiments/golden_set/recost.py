#!/usr/bin/env python3
"""Re-price a results.jsonl with the fixed off-peak price table in backend/llm/pricing.py.

Runs recorded during DeepSeek peak hours carry peak-rate `cost_cny` in their ledger. Token counts do not
depend on the hour, so this recomputes every call from tokens and prints per-category and per-model means.

  ./dev py docs/experiments/golden_set/recost.py docs/experiments/exports/golden-heldout-react.jsonl ...
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from backend.llm.pricing import call_cost_cny


def main(paths: list[str]) -> None:
    for path in paths:
        rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        by_cat: dict[str, list[float]] = defaultdict(list)
        by_model: dict[str, float] = defaultdict(float)
        old_total = new_total = tokens = 0.0
        for r in rows:
            cost = 0.0
            for c in (r.get("ledger") or {}).get("calls") or []:
                one = call_cost_cny(
                    c.get("provider") or "", c.get("model") or "",
                    prompt_tokens=c.get("prompt_tokens") or 0,
                    completion_tokens=c.get("completion_tokens") or 0,
                    cache_hit_tokens=c.get("cache_hit_tokens"),
                )
                cost += one
                by_model[c.get("model") or "?"] += one
                old_total += c.get("cost_cny") or 0.0
                tokens += (c.get("prompt_tokens") or 0)
            by_cat[r["category"]].append(cost)
            new_total += cost
        n = len(rows)
        print(f"{path}: {n} runs  recorded ¥{old_total / n:.5f}/run  off-peak ¥{new_total / n:.5f}/run  "
              f"prompt tokens/run {tokens / n:.0f}")
        print("  by category:", {k: f"¥{sum(v) / len(v):.5f} (n={len(v)})" for k, v in sorted(by_cat.items())})
        print("  by model share:", {k: f"{v / new_total:.0%}" for k, v in by_model.items()})


if __name__ == "__main__":
    main(sys.argv[1:])
