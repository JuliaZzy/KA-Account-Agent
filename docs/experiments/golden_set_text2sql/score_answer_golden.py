#!/usr/bin/env python3
"""Programmatic checks for the ANSWER stage. No model judge anywhere.

A case = user_message + materials (exactly the text the answer model sees) + checks.
Global checks (every case):
  non_empty            answer has text
  numbers_grounded     every number in the answer equals a number found in materials or the user message
                       (token-level numeric equality: 12 != 120; 551.10 == 551.1; "3%" may match 3 or 0.03)
  tokens_grounded      every SKU / order number / e-mail in the answer appears in materials or the user message
  chart_claim          answer may not say a chart is drawn unless materials has a non-empty charts_drawn
Per-case checks: must_include_all / must_include_any / must_ack / forbid_claim / first_sentence /
                 must_mention / forbid_regex   (see build_answer_golden.py)
Usage:
  python score_answer_golden.py --cases answer_golden_v1.jsonl --results answers.jsonl
  python score_answer_golden.py --cases answer_golden_v1.jsonl --selftest
"""
import argparse, collections, json, re, sys, unicodedata
from decimal import Decimal

NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
TOK_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\b[A-Za-z]{2,}(?:-[A-Za-z0-9]+)+\b")
NEG = re.compile(r"没|未|无法|不能|尚未|并未|不会|不曾|失败|出错|报错|拿不到|查不到|不确定|不要|请勿|不是")
FAIL = re.compile(
    r"没(有)?(能|法|办法)?(拿到|查到|查成功|成功|完成|获取|取到|读到|读取|办成|处理|执行|画|生成|起草|发送|发出|回答|办|做)"
    r"|未(能)?(拿到|查到|成功|完成|获取|处理|执行|画|生成|起草|发送|发出|回答|做)"
    r"|还没(有)?(起草|画|查|生成|发送|发出|读|拿到|做|处理)"
    r"|无法|失败|出错|报错|报了错|查不到|拿不到|超时|不确定|没法|没办法|跳过|丢弃|没能|未能|尚未|并未|没有确认|未确认|确认不了")
PLAN_ACK = re.compile(r"(没有|没|未|无法|没能|不能|还没|尚未)(能)?(生成|执行|查|起草|完成|处理|办成|开始|发)|重试|再试|重新(发|提|来)|稍后|出了(点)?问题|没办法")
CLAIMS = {
    "query": r"已(经)?(为你|帮你|成功)?(查询?到?|查完|查过|查好|检索|核对|获取|拿到|取到)|查(过|完|好|到)了|查询(完成|结果如下)|结果如下",
    "draft": r"已(经)?(为你|帮你|成功)?(起草|生成|创建|拟好|草拟|准备好|做好)|草稿已|起草(完成|好了)",
    "sent": r"已(经)?(成功)?(发送|发出|发信|发给|寄出|送达|送出)|发送(成功|完成)|发出去了|邮件已",
    "report": r"(月报|报告|报表)(已|已经)(生成|完成|起草|做好)|已(经)?(为你|帮你|成功)?(生成|起草|完成|做好)(了)?(这份|该|一份)?(月报|报告|报表)",
    "read": r"已(经)?(成功)?(读取|读完|解析|读过|阅读|识别|提取)|读取完成|解析完成",
}
CHART_CLAIM = [
    r"(图表?|饼图|柱状图|折线图|条形图)[^，。；\n]{0,3}(已|已经)[^，。；\n]{0,3}(画|生成|绘制|完成|展示|呈现)",
    r"(已|已经)(为你)?(画|生成|绘制|做)(好|出|成)?(了)?[^，。；\n]{0,8}(图表?|饼图|柱状图|折线图|条形图)",
    r"(见|看|查看|参考)(右侧|下方|上方|旁边)?(的)?(图表?|饼图|柱状图|折线图)",
    r"右侧(的)?(图表?|饼图|柱状图|折线图)", r"(在)?右侧(查看|可见|展示|看到)",
]

def norm(s):
    return unicodedata.normalize("NFKC", re.sub("[①-⑳]", " ", s or ""))

def strip_tokens(s):
    """-> (text with SKU/e-mail tokens blanked, set of lowercase tokens). Tokens need a digit (SKU, PO no.) or an @."""
    toks = set()
    def sub(m):
        t = m.group(0)
        if "@" in t or re.search(r"\d", t): toks.add(t.lower()); return " " * len(t)
        return t
    return TOK_RE.sub(sub, s), toks

def numbers(s, keep_pct=False):
    s, _ = strip_tokens(norm(s))
    s = re.sub(r"(?m)^\s*[（(]?\d{1,2}[)）.、]", " ", s)          # list markers
    out = []
    for m in NUM_RE.finditer(s):
        pct = s[m.end():m.end() + 1] == "%"
        out.append((Decimal(m.group(0).replace(",", "")), pct, m.group(0)))
    return out

def clauses(text):
    return [c for sent in re.split(r"(?<=[。！？!?\n])", norm(text)) for c in re.split(r"[，,、；;：:]", sent)]

def sentences(text):
    return [s for s in re.split(r"(?<=[。！？!?\n])", norm(text)) if s.strip()]

def has_claim(text, subject, names):
    pats = [CLAIMS[n] for n in names]
    for c in clauses(text):
        if NEG.search(c): continue
        if subject and not re.search(subject, c): continue
        if any(re.search(p, c) for p in pats): return c.strip()
    return None

def run_checks(case, answer, materials=None):
    """-> list of (name, ok, detail)

    `materials` overrides the case text when a result row records the string that was
    actually sent. The default is the production user turn stored on the case.
    """
    out, ans = [], norm(answer or "")
    user = norm(case["user_message"])
    mats = norm(case["materials"] if materials is None else materials)
    out.append(("non_empty", len(ans.strip()) >= 4, "" if len(ans.strip()) >= 4 else "empty answer"))
    gn = {n for n, _, _ in numbers(mats)} | {n for n, _, _ in numbers(user)}
    bad = []
    for v, pct, raw in numbers(ans):
        if v in gn or (pct and (v / 100) in gn): continue
        bad.append(raw + ("%" if pct else ""))
    out.append(("numbers_grounded", not bad, "not in materials: " + ", ".join(bad) if bad else ""))
    _, at = strip_tokens(ans); _, mt = strip_tokens(mats); _, ut = strip_tokens(user)
    badt = sorted(at - mt - ut)
    out.append(("tokens_grounded", not badt, "not in materials: " + ", ".join(badt) if badt else ""))
    drawn = bool(re.search(r"charts_drawn\"?\s*[:=]\s*\[\s*[\"']", mats))
    hit = None
    if not drawn:
        for c in clauses(ans):
            if NEG.search(c): continue
            if any(re.search(p, c) for p in CHART_CLAIM): hit = c.strip(); break
    out.append(("chart_claim", hit is None, f"claims a chart without charts_drawn: 「{hit}」" if hit else ""))
    for k in case["checks"]:
        t = k["type"]; name = k.get("name", t)
        if t in ("must_include_all", "must_include_any"):
            an = {n for n, _, _ in numbers(ans)}
            def present(v):
                try: return Decimal(v.replace(",", "")) in an
                except Exception: return v.lower() in ans.lower()
            res = [present(v) for v in k["values"]]
            ok = all(res) if t.endswith("all") else any(res)
            out.append((name, ok, "" if ok else "missing: " + ", ".join(v for v, r in zip(k["values"], res) if not r)))
        elif t == "must_ack":
            ok = any(re.search(k["subject"], s) and FAIL.search(s) for s in sentences(ans))
            out.append((name, ok, "" if ok else f"never says the {k['subject']} part failed / was not done"))
        elif t == "must_ack_plan":
            ok = bool(PLAN_ACK.search(ans))
            out.append((name, ok, "" if ok else "does not say nothing was run"))
        elif t == "forbid_claim":
            c = has_claim(ans, k.get("subject"), k["claims"])
            out.append((name, c is None, f"claims completion: 「{c}」" if c else ""))
        elif t == "first_sentence":
            ok = ans.strip().startswith(k["text"])
            out.append((name, ok, "" if ok else f"first sentence must be 「{k['text']}」"))
        elif t == "must_mention":
            ok = bool(re.search(k["pattern"], ans))
            out.append((name, ok, "" if ok else "required statement missing"))
        elif t == "forbid_regex":
            m = next((re.search(p, ans) for p in k["patterns"] if re.search(p, ans)), None)
            out.append((name, m is None, f"forbidden wording: 「{m.group(0)}」" if m else ""))
    return out

def score(cases, results):
    by = collections.defaultdict(list)
    for r in results: by[r["id"]].append(r)
    kinds = collections.defaultdict(lambda: [0, 0]); fab = claim = ctl_ok = ctl_n = runs = 0
    for c in cases:
        for r in by.get(c["id"], []):
            res = run_checks(c, r.get("answer"), r.get("materials"))
            failed = [(n, d) for n, ok, d in res if not ok]
            passed = not failed
            if c.get("expect_fail"):
                ctl_n += 1; ctl_ok += (not passed)
                print(f"  {c['id']:8s} control: checker {'CAUGHT' if not passed else 'MISSED'} the bad answer")
                continue
            runs += 1; kinds[c["kind"]][0] += 1; kinds[c["kind"]][1] += passed
            fab += any(n in ("numbers_grounded", "tokens_grounded") for n, _ in failed)
            claim += any(n in ("chart_claim",) or n.startswith("no_claim") for n, _ in failed)
            print(f"  {c['id']:8s} {'pass' if passed else 'FAIL'}" + "".join(f"\n      - {n}: {d}" for n, d in failed))
    print("\nper kind (runs / pass):")
    for k, (n, p) in kinds.items(): print(f"  {k:14s} {n:3d} {p:3d}")
    print(f"\nruns with an ungrounded number/SKU/e-mail: {fab}/{runs}   runs claiming something not done: {claim}/{runs}")
    if ctl_n: print(f"controls caught: {ctl_ok}/{ctl_n}")
    return runs, sum(v[1] for v in kinds.values()), ctl_n, ctl_ok

def selftest(cases):
    bad = 0
    for c in cases:
        if c.get("canned_good"):
            res = run_checks(c, c["canned_good"]); f = [n for n, ok, _ in res if not ok]
            if f: bad += 1; print(f"[{c['id']}] canned_good should pass but failed: {f}")
        for b in c.get("canned_bad", []):
            res = run_checks(c, b["answer"]); f = {n for n, ok, _ in res if not ok}
            expect = set(b.get("expect") or [])
            if not f: bad += 1; print(f"[{c['id']}] bad answer slipped through: {b['answer'][:50]}")
            elif expect and not expect <= f:
                missing = sorted(expect - f)
                bad += 1
                print(f"[{c['id']}] missing expected {missing}; failed {sorted(f)}: {b['answer'][:40]}")
    n_bad = sum(len(c.get("canned_bad", [])) for c in cases)
    print(f"selftest: {len(cases)} cases, {sum(1 for c in cases if c.get('canned_good'))} good answers, {n_bad} bad answers, problems: {bad}")
    return bad

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cases", required=True)
    ap.add_argument("--results"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    cases = [json.loads(l) for l in open(a.cases, encoding="utf-8") if l.strip()]
    if a.selftest: sys.exit(1 if selftest(cases) else 0)
    score(cases, [json.loads(l) for l in open(a.results, encoding="utf-8") if l.strip()])
