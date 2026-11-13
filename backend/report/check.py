"""Deterministic gates on a report draft, run before the LLM reviewer.

These catch leftovers the writer should never have emitted: a budget error
code, a private number that is not in the evidence JSON, a requested calendar
period that never appears, or a required chapter heading that is missing.
Chart citations are applied in ``turn.place_charts_in_sections``; this module
only reports what still looks wrong after that.
"""

from __future__ import annotations

import re

from ..services.run_names import EntryErrorCode

_REQUIRED_TITLES = ("关键指标", "摘要", "业绩表现", "库存情况", "下一周期计划")
_HEADING = re.compile(r"^##\s+(?:\d+\.\s+)?(.+)$", re.M)
_PERIOD = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")
_ISO_PERIOD = re.compile(r"(20\d{2})-(\d{1,2})")
# Long or decimal numbers are the ones that look like money, qty, or percents.
# Years and small counts (4 张单, 8.5 天) stay out: they fire too often.
_NUMBER = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{2,}|\d{5,})(?![\d])")
_YEAR = re.compile(r"^20\d{2}$")
_BUDGET = re.compile(re.escape(EntryErrorCode.COST_BUDGET_EXCEEDED))


def strip_budget_errors(text: str) -> str:
    cleaned = _BUDGET.sub("", text or "")
    return re.sub(r"[ \t]+\n", "\n", cleaned).strip()


def requested_period(message: str) -> tuple[int, int] | None:
    match = _PERIOD.search(message or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _period_strings(year: int, month: int) -> tuple[str, ...]:
    return (
        f"{year}年{month}月",
        f"{year}年{month:02d}月",
        f"{year}-{month:02d}",
        f"{year}-{month}",
    )


def _normalize_number(raw: str) -> str:
    return raw.replace(",", "")


def _evidence_numbers(evidence: str) -> set[str]:
    return {_normalize_number(item) for item in _NUMBER.findall(evidence or "")}


def heading_titles(report: str) -> list[str]:
    titles: list[str] = []
    for match in _HEADING.finditer(report or ""):
        title = match.group(1).strip()
        if title:
            titles.append(title)
    return titles


def hard_issues(
    report: str,
    *,
    message: str = "",
    evidence: str = "",
) -> list[dict[str, str]]:
    """Issues the LLM reviewer does not need to rediscover."""
    issues: list[dict[str, str]] = []
    text = report or ""
    if _BUDGET.search(text):
        issues.append({
            "section": "摘要",
            "issue_type": "format",
            "severity": "major",
            "description": "正文里出现了内部错误码 COST_BUDGET_EXCEEDED",
            "suggestion": "删掉错误码，改成这一节缺了什么",
        })
    present = heading_titles(text)
    if present:
        have = set(present)
        for title in _REQUIRED_TITLES:
            if title not in have:
                issues.append({
                    "section": title,
                    "issue_type": "incomplete",
                    "severity": "major",
                    "description": f"缺少「{title}」这一节",
                    "suggestion": f"按大纲补上{title}，缺数据就写缺了什么",
                })
    period = requested_period(message)
    if period is not None and not any(token in text for token in _period_strings(*period)):
        year, month = period
        issues.append({
            "section": "关键指标",
            "issue_type": "logic",
            "severity": "major",
            "description": f"用户要的是{year}年{month}月，正文里没有这个期间",
            "suggestion": f"各节按{year}年{month}月来写，不要换成别的月份",
        })
    allowed = _evidence_numbers(evidence)
    if allowed:
        seen: set[str] = set()
        for raw in _NUMBER.findall(text):
            key = _normalize_number(raw)
            if key in seen or _YEAR.match(key) or key in allowed:
                continue
            seen.add(key)
            issues.append({
                "section": "业绩表现",
                "issue_type": "hallucination",
                "severity": "critical",
                "description": f"数字 {raw} 在查询结果和洞察里找不到",
                "suggestion": "删掉这个数字，或改成证据里有的口径",
            })
    return issues


def apply_hard_fixes(report: str) -> str:
    """Code-only cleanup. Chart placement stays in ``turn``; this only strips leak."""
    return strip_budget_errors(report)
