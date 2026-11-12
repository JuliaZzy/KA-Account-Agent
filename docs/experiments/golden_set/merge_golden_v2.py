"""Merge golden_v2 (core) and golden_hard_v2 (hard text2sql / report) into golden_all_v2.jsonl.

Every row gets a `split`:
  core     the 39 regression cases
  dev      hard cases we are allowed to tune against (odd t2s ids + rpt-09)
  heldout  the other hard cases (even t2s ids + rpt-08): run for a release check, at most once per
           version, never to debug. run_golden.py leaves this split out unless asked for by name.
Run: python3 merge_golden_v2.py
"""
import json
import re
from pathlib import Path

here = Path(__file__).parent


def split_of(case_id: str) -> str:
    m = re.fullmatch(r"t2s-(\d+)", case_id)
    if m:
        return "dev" if int(m.group(1)) % 2 else "heldout"
    return {"rpt-09": "dev", "rpt-08": "heldout"}[case_id]


rows = [dict(json.loads(l), split="core") for l in open(here / "golden_v2.jsonl", encoding="utf-8")]
rows += [dict(json.loads(l), split=split_of(json.loads(l)["id"])) for l in open(here / "golden_hard_v2.jsonl", encoding="utf-8")]
ids = [r["id"] for r in rows]
assert len(ids) == len(set(ids)), "duplicate case ids across files"
(here / "golden_all_v2.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
print({s: sum(r["split"] == s for r in rows) for s in ("core", "dev", "heldout")})
