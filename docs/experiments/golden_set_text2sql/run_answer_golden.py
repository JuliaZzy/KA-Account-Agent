#!/usr/bin/env python3
"""Feed each golden case to the ANSWER stage and save the replies for score_answer_golden.py.

The answer stage is called through an adapter you point at, because only your code knows how the real
ANSWER prompt and materials are assembled:

    def answer(user_message: str, materials: str, case: dict) -> str:
        # `materials` is already the user turn compose_answer would send (system prompt and
        # history aside): plan note, opening line, then the result envelopes. Send that
        # string. Return a str, or {"answer": str, "materials": str} if you rebuilt it.

    python3 docs/experiments/golden_set_text2sql/run_answer_golden.py --adapter my_adapter:answer --repeats 5 --out answers.jsonl
    python3 docs/experiments/golden_set_text2sql/score_answer_golden.py --cases docs/experiments/golden_set_text2sql/answer_golden_v1.jsonl --results answers.jsonl

Cases with a fixed_answer (the 12 -> 120 control) are not sent to the model; they test the scorer.
--canned good|bad runs the scorer's own sample answers (no model, no cost) as a dry run.
The adapter may be sync or async. A dict return may replace `materials` for the scorer.
"""
import argparse, asyncio, importlib, importlib.util, inspect, json, sys, time
from pathlib import Path

def load_fn(spec: str):
    mod, name = spec.split(":")
    path = Path(mod)
    if path.suffix == ".py":
        loaded = importlib.util.spec_from_file_location(path.stem, path)
        if loaded is None or loaded.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(loaded)
        loaded.loader.exec_module(module)
        return getattr(module, name)
    sys.path.insert(0, ".")
    return getattr(importlib.import_module(mod), name)

def call(fn, user, mats, case):
    # The model client drives its own loop. Do not call it from inside asyncio.run.
    r = fn(user, mats, case)
    return asyncio.run(r) if inspect.isawaitable(r) else r

def main(a):
    cases = [json.loads(l) for l in open(a.cases, encoding="utf-8") if l.strip()]
    if a.only: cases = [c for c in cases if c["id"] in a.only.split(",")]
    fn = None
    if a.adapter:
        fn = load_fn(a.adapter)
    rows = []
    for c in cases:
        reps = 1 if (c.get("fixed_answer") or a.canned) else a.repeats
        for i in range(1, reps + 1):
            row = {"id": c["id"], "repeat": i}
            t0 = time.monotonic()
            fed = c["materials"]
            try:
                if c.get("fixed_answer"): row["answer"] = c["fixed_answer"]
                elif a.canned == "good": row["answer"] = c["canned_good"]
                elif a.canned == "bad": row["answer"] = c["canned_bad"][0]["answer"]
                else:
                    ans = call(fn, c["user_message"], c["materials"], c)
                    if isinstance(ans, dict):
                        fed = str(ans.get("materials", fed))
                        ans = ans.get("answer")
                    if not isinstance(ans, str): raise TypeError(f"adapter returned {type(ans).__name__}, expected str")
                    row["answer"] = ans
                row["materials"] = fed
            except Exception as exc:   # an adapter crash is recorded, not hidden; scorer reports it as an empty answer
                row["answer"] = ""; row["error"] = f"{type(exc).__name__}: {exc}"[:300]
            row["seconds"] = round(time.monotonic() - t0, 2)
            rows.append(row)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    print(f"{len(rows)} answers written to {a.out}; {sum(1 for r in rows if r.get('error'))} adapter errors")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(Path(__file__).with_name("answer_golden_v1.jsonl")))
    ap.add_argument("--out", required=True); ap.add_argument("--adapter", help="module:function")
    ap.add_argument("--repeats", type=int, default=3); ap.add_argument("--only")
    ap.add_argument("--canned", choices=["good", "bad"])
    a = ap.parse_args()
    if not a.adapter and not a.canned: ap.error("give --adapter module:function (or --canned good|bad for a dry run)")
    main(a)
