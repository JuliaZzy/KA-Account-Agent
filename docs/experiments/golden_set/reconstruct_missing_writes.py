"""Reconstruct the four chapter-write calls that never hit the run ledger.

Those writes omit `trace`, so vendor usage was never stored. This rebuilds each
prompt with the same helpers the report path uses, takes chapter prose from the
first review (the draft before revise), and scales the local token estimator to
the ledgered 问题与风险 write on the same run — that call stored the real prompt.

  ./dev py docs/experiments/golden_set/reconstruct_missing_writes.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend.llm.calls import _estimate_tokens, _prompt_text
from backend.llm.pricing import call_cost_cny
from backend.memory import attach
from backend.report.prompt import SECTIONS, WRITING_PROMPT
from backend.report.turn import (
    _asks_for,
    _assign_chart_ids,
    _body_for_summary,
    _fill_risks,
    _insights_for,
    _section_payload,
    asks_from_payloads,
    merge_extra_asks,
    parse_plan,
    parse_risk_plan,
    strip_chart_refs,
)

ROOT = Path("docs/experiments/exports")
FIRST_FOUR = ("metrics", "performance", "inventory", "next")
FILES = [
    ("rpt-01#1", ROOT / "rptfix-raw/rpt-01-1.json"),
    ("rpt-01#3", ROOT / "rptfix-raw/rpt-01-3.json"),
    ("plan-02#1", ROOT / "rptfix-raw/plan-02-1.json"),
    ("d-w5 rpt-01#1", ROOT / "d-w5-pre-raw/rpt-01-1.json"),
    ("d-w5 plan-02#1", ROOT / "d-w5-pre-raw/plan-02-1.json"),
]


def unwrap(resp: object) -> object:
    if isinstance(resp, dict) and "json" in resp:
        inner = resp["json"]
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                return inner
        return inner
    return resp


def schema_name(mc: dict) -> str:
    return str(((mc.get("request_json") or {}).get("schema") or {}).get("name") or "")


def traces(raw: dict) -> list[dict]:
    parent_id = (raw["trace"].get("run") or {}).get("id")
    out = [raw["trace"]]
    for cid, child in (raw.get("children") or {}).items():
        if cid == parent_id:
            continue
        tr = (child or {}).get("trace")
        if isinstance(tr, dict):
            out.append(tr)
    return out


def report_trace(raw: dict) -> dict:
    for tr in traces(raw):
        if (tr.get("run") or {}).get("run_type") == "report":
            return tr
    raise KeyError("no report child")


def user_message(report: dict) -> str:
    payload = report.get("run", {}).get("input_json") or {}
    text = str(payload.get("message") or "").strip()
    inner = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    topic = str(inner.get("topic") or "").strip()
    req = str(inner.get("requirements") or "").strip()
    if text:
        return text[:4000]
    if topic and req:
        return f"{topic}，{req}"[:4000]
    return topic or "（无文字）"


def plan_decision(report: dict) -> dict:
    for step in report.get("steps") or []:
        purpose = str((step.get("input_json") or {}).get("purpose") or "")
        out = step.get("output_json") or {}
        if purpose == "report.plan" and isinstance(out, dict) and "sections" in out:
            return out
        if isinstance(out, dict) and "sections" in out and "questions" in out:
            return out
    raise KeyError("no report.plan step")


def data_results(raw: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for tr in traces(raw):
        run = tr.get("run") or {}
        if run.get("run_type") != "data":
            continue
        inp = run.get("input_json") or {}
        task_id = str(inp.get("task_id") or "")
        out = run.get("output_json") if isinstance(run.get("output_json"), dict) else {}
        payload = out.get("payload") if isinstance(out.get("payload"), dict) else out
        if task_id and isinstance(payload, dict):
            found[task_id] = payload
    return found


def extra_pairs(raw: dict, first_wave: set[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tr in traces(raw):
        run = tr.get("run") or {}
        if run.get("run_type") != "data":
            continue
        inp = run.get("input_json") or {}
        task_id = str(inp.get("task_id") or "")
        question = str((inp.get("input") or {}).get("question") or "")
        if not task_id or task_id in first_wave or task_id in seen:
            continue
        seen.add(task_id)
        pairs.append((task_id, question))
    return pairs


def classified_calls(report: dict) -> list[dict]:
    rows = []
    for mc in report.get("model_calls") or []:
        rq = mc.get("request_json") or {}
        system = str(rq.get("system") or "")
        name = schema_name(mc)
        match = re.search(r"当前章节：([^\n]+)", system)
        title = match.group(1).strip() if match else ""
        rows.append({
            "mc": mc, "schema": name, "title": title,
            "inner": unwrap(mc.get("response_json")),
            "request": rq,
        })
    return rows


def charts_for_sections(calls: list[dict], planned) -> dict[str, list[dict]]:
    used: set[str] = set()
    owned: dict[str, list[dict]] = {}
    chart_calls = [item for item in calls if item["schema"] == "report_charts"]
    chart_sections = [section for section in planned if section.chart]
    for section, item in zip(chart_sections, chart_calls):
        inner = item["inner"] if isinstance(item["inner"], dict) else {}
        made = []
        for spec in inner.get("charts") or []:
            if not isinstance(spec, dict):
                continue
            made.append({
                "id": spec.get("id") or "",
                "title": spec.get("title") or "",
                "type": spec.get("type") or "",
            })
        owned[section.id] = _assign_chart_ids(made, used)
    return owned


def first_review_bundle(report: dict) -> tuple[str, dict]:
    for mc in report.get("model_calls") or []:
        if schema_name(mc) != "report_review":
            continue
        system = str((mc.get("request_json") or {}).get("system") or "")
        report_match = re.search(r"已写报告：(.+?)\n- 查询结果、洞察、新闻、图表：", system, re.S)
        evidence_match = re.search(r"查询结果、洞察、新闻、图表：(.+?)\n- 上一轮", system, re.S)
        text = (report_match.group(1) if report_match else "").strip()
        evidence: dict = {}
        if evidence_match:
            try:
                parsed = json.loads(evidence_match.group(1))
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                evidence = parsed
        return text, evidence
    return "", {}


def split_chapters(report_text: str) -> dict[str, str]:
    title_to_id = {str(section["title"]): str(section["id"]) for section in SECTIONS}
    parts = re.split(r"^##\s+\d+\.\s+", report_text, flags=re.M)
    heads = re.findall(r"^##\s+\d+\.\s+(.+)$", report_text, flags=re.M)
    out: dict[str, str] = {}
    for title, body in zip(heads, parts[1:]):
        sid = title_to_id.get(title.strip())
        if not sid:
            continue
        text = body.strip()
        heading = title.strip()
        if text.startswith(heading):
            text = text[len(heading):].lstrip("\n")
        out[sid] = text.strip()
    return out


def found_from_news(news: list[dict], searches: list[str]) -> list:
    from backend.report.turn import _Search

    wanted = {query.casefold() for query in searches}
    found = []
    for item in news:
        query = str(item.get("query") or "")
        if query.casefold() not in wanted:
            continue
        results = []
        for row in item.get("results") or []:
            if not isinstance(row, dict):
                continue
            results.append({
                "title": str(row.get("title") or "")[:160],
                "snippet": str(row.get("snippet") or "")[:300],
                "url": str(row.get("url") or "")[:300],
            })
        found.append(_Search(query=query, ok=True, results=results, error=None))
    return found


def insights_from_evidence(evidence: dict) -> dict[str, list[dict]]:
    by_section: dict[str, list[dict]] = {}
    for item in evidence.get("insights") or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("section") or "")
        if not sid:
            continue
        cleaned = {key: value for key, value in item.items() if key != "section"}
        by_section.setdefault(sid, []).append(cleaned)
    return by_section


def completion_json(content: str, key_points: list[str] | None = None) -> str:
    return json.dumps(
        {"content": content, "key_points": key_points or []},
        ensure_ascii=False,
    )


def ds_cost(prompt: int, completion: int, hit: int | None = None) -> float:
    return call_cost_cny(
        "deepseek", "deepseek-flash",
        prompt_tokens=prompt, completion_tokens=completion,
        cache_hit_tokens=hit,
    )


def writing_system(message: str, section) -> str:
    return attach(WRITING_PROMPT.format(
        message=message, section_title=section.title, section_brief=section.brief,
    ), "")


def estimate_prompt(system: str, payload: str) -> int:
    return _estimate_tokens(_prompt_text(system, [{"role": "user", "content": payload}]))


def reconstruct(path: Path) -> dict:
    raw = json.loads(path.read_text())
    report = report_trace(raw)
    decision = plan_decision(report)
    planned = parse_plan({"title": decision.get("title"), "sections": decision.get("sections")})
    pairs = [
        (str(task_id), str(question))
        for task_id, question in (decision.get("questions") or [])
        if task_id and question
    ]
    results = data_results(raw)
    asked, by_section, _failures = asks_from_payloads(planned, results, pairs)
    calls = classified_calls(report)
    charts_for = charts_for_sections(calls, planned)
    draft_text, evidence = first_review_bundle(report)
    chapters = split_chapters(draft_text)
    insights_by_section = insights_from_evidence(evidence)
    news = list(evidence.get("news") or [])
    message = user_message(report)
    found_all = found_from_news(news, [
        query for section in planned for query in section.searches
    ])

    known_write = next(
        item for item in calls
        if item["schema"] == "report_section" and item["title"] == "问题与风险"
    )
    known_inner = known_write["inner"] if isinstance(known_write["inner"], dict) else {}
    known_actual_p = int(known_write["mc"].get("prompt_tokens") or 0)
    known_actual_c = int(known_write["mc"].get("completion_tokens") or 0)
    known_hit = int(known_write["mc"].get("cache_hit_tokens") or 0)
    known_content = str(known_inner.get("content") or "")
    known_points = [str(p) for p in (known_inner.get("key_points") or []) if str(p).strip()]
    known_request = known_write["request"] if isinstance(known_write["request"], dict) else {}
    known_actual_prompt_text = _prompt_text(
        str(known_request.get("system") or ""),
        list(known_request.get("messages") or []),
    )
    tokenizer_scale = (
        known_actual_p / _estimate_tokens(known_actual_prompt_text)
        if known_actual_prompt_text else 1.0
    )
    completion_scale = (
        known_actual_c / _estimate_tokens(completion_json(known_content, known_points))
        if known_content or known_points else 1.0
    )

    risk_call = next((item for item in calls if item["schema"] == "risk_plan"), None)
    risk_decision = parse_risk_plan(risk_call["inner"] if risk_call else {})
    planned_with_risks = _fill_risks(planned, risk_decision.questions, risk_decision.searches)
    merge_extra_asks(
        asked, by_section, extra_pairs(raw, {task_id for task_id, _question in pairs}),
        results, section_id="risks",
    )
    if risk_decision.searches:
        found_all = found_from_news(news, [
            query for section in planned_with_risks for query in section.searches
        ])

    risks = next(section for section in planned_with_risks if section.id == "risks")
    snapshot_texts = {
        sid: chapters[sid]
        for sid in FIRST_FOUR
        if chapters.get(sid)
    }
    risks_payload = _section_payload(
        risks, _asks_for(risks, by_section), found_from_news(news, risks.searches),
        [], _insights_for(risks, insights_by_section),
        _body_for_summary(planned_with_risks, snapshot_texts),
    )
    risks_est_p = estimate_prompt(writing_system(message, risks), risks_payload)
    recon_quality = (
        risks_est_p / _estimate_tokens(known_actual_prompt_text)
        if known_actual_prompt_text else 1.0
    )
    # The four chapter writes have no「已写章节」block. Scaling off a reconstructed
    # risks prompt would bake that unique suffix into every chapter. Use the
    # vendor/estimator ratio on the stored risks prompt instead.
    scale_p = tokenizer_scale

    missing = []
    for section in planned:
        if section.id not in FIRST_FOUR:
            continue
        payload = _section_payload(
            section, _asks_for(section, by_section), found_from_news(news, section.searches),
            charts_for.get(section.id, []) if section.chart else [],
            _insights_for(section, insights_by_section),
        )
        content = chapters.get(section.id) or ""
        est_p = estimate_prompt(writing_system(message, section), payload)
        est_c = _estimate_tokens(completion_json(content, []))
        prompt = max(1, round(est_p * scale_p))
        # Short drafts (often just chart refs) did not carry key_points; longer
        # chapters get the known write's completion density, which includes them.
        if len(content) < 80:
            completion = max(1, round(est_c * completion_scale))
        else:
            completion = max(1, round(known_actual_c * len(content) / max(1, len(known_content))))
        completion_from_estimator = max(1, round(est_c * completion_scale))
        hit_rest = min(known_hit, prompt)
        missing.append({
            "section": section.title,
            "id": section.id,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "completion_tokens_estimator": completion_from_estimator,
            "prompt_tokens_raw_estimate": est_p,
            "completion_tokens_raw_estimate": est_c,
            "content_chars": len(content),
            "cost_cny_no_cache": round(ds_cost(prompt, completion, 0), 6),
            "cost_cny_hit_like_risks": round(ds_cost(prompt, completion, hit_rest), 6),
        })

    total_p = sum(item["prompt_tokens"] for item in missing)
    total_c = sum(item["completion_tokens"] for item in missing)
    first, *rest = missing
    cost_mixed = (
        ds_cost(first["prompt_tokens"], first["completion_tokens"], 0)
        + sum(ds_cost(item["prompt_tokens"], item["completion_tokens"], min(known_hit, item["prompt_tokens"]))
              for item in rest)
    ) if missing else 0.0
    return {
        "path": str(path),
        "calibration": {
            "known_section": "问题与风险",
            "actual_prompt_tokens": known_actual_p,
            "actual_completion_tokens": known_actual_c,
            "actual_cache_hit_tokens": known_hit,
            "actual_content_chars": len(known_content),
            "actual_key_points": len(known_points),
            "reconstructed_risks_prompt_tokens": risks_est_p,
            "stored_prompt_estimated_tokens": _estimate_tokens(known_actual_prompt_text),
            "tokenizer_scale": round(tokenizer_scale, 4),
            "reconstruction_over_stored": round(recon_quality, 4),
            "scale_prompt": round(scale_p, 4),
            "scale_completion": round(completion_scale, 4),
        },
        "missing_writes": missing,
        "totals": {
            "prompt_tokens": total_p,
            "completion_tokens": total_c,
            "cost_cny_no_cache": round(ds_cost(total_p, total_c, 0), 6),
            "cost_cny_first_cold_rest_hit": round(cost_mixed, 6),
            "cost_cny_hit_like_risks": round(
                sum(item["cost_cny_hit_like_risks"] for item in missing), 6,
            ),
        },
        "usage_source": "reconstructed_then_scaled_to_ledgered_write",
        "news_queries": len(found_all),
        "insight_sections": {sid: len(items) for sid, items in insights_by_section.items()},
    }


def main() -> None:
    out = {}
    for label, path in FILES:
        if not path.exists():
            print("missing", path)
            continue
        result = reconstruct(path)
        out[label] = result
        cal = result["calibration"]
        tot = result["totals"]
        print("\n" + "=" * 72)
        print(label)
        print(
            f"  校准  actual p/c {cal['actual_prompt_tokens']}/{cal['actual_completion_tokens']}  "
            f"recon_risks_est {cal['reconstructed_risks_prompt_tokens']}  "
            f"stored_est {cal['stored_prompt_estimated_tokens']}  "
            f"recon/stored {cal['reconstruction_over_stored']}  "
            f"scale_p {cal['scale_prompt']}"
        )
        print(
            f"{'章节':<10} {'prompt':>8} {'compl':>7} {'¥ 无缓存':>10} "
            f"{'¥ 首冷其余命中':>14} {'正文字':>8}"
        )
        for item in result["missing_writes"]:
            print(
                f"{item['section']:<10} {item['prompt_tokens']:>8} {item['completion_tokens']:>7} "
                f"{item['cost_cny_no_cache']:>10.4f} {item['cost_cny_hit_like_risks']:>14.4f} "
                f"{item['content_chars']:>8}"
            )
        print(
            f"{'四节合计':<10} {tot['prompt_tokens']:>8} {tot['completion_tokens']:>7} "
            f"{tot['cost_cny_no_cache']:>10.4f} {tot['cost_cny_first_cold_rest_hit']:>14.4f}"
        )
    dest = ROOT / "report-missing-writes.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print("\nwrote", dest)


if __name__ == "__main__":
    main()
