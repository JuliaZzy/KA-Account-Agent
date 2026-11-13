"""Bind one query to one chart, then drop a drawing that does not match the bound rows.

The planner only writes questions. Roles are tagged here after the plan: a category pie,
a period trend, or a product bar. An empty bound dataset skips the model; a drawing that
fails the check is dropped, not sent to code.fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ..text2sql.text2sql import no_matching_records
from .analyze import column_kinds, value_kind
from .metrics import period_from_message, uses_purchase_orders

ChartRole = Literal["pie_category", "trend", "bar"]

_PERIOD_AXIS = re.compile(
    r"^(?:20\d{2})(?:-(?:0[1-9]|1[0-2])|年(?:0?[1-9]|1[0-2])月?)$"
)
_PRODUCT_COLS = (
    "product_name", "productname", "product_sku", "sku", "item_name", "goods_name",
    "产品名称", "产品",
)
_CATEGORY_MARKERS = ("category", "品类", "分类")
_PIE_MARKERS = ("饼", "占比")
_TREND_MARKERS = (
    "近六", "近五", "近半", "六个报告期", "五个报告期",
    "按月", "按报告期", "各月", "趋势",
)


@dataclass(frozen=True)
class ChartDecision:
    dataset_id: str
    section_id: str
    question: str
    role: ChartRole
    status: str
    reason: str = ""
    period: str | None = None
    chart_id: str = ""
    title: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "section_id": self.section_id,
            "question": self.question,
            "role": self.role,
            "status": self.status,
            "reason": self.reason,
            "period": self.period,
            "chart_id": self.chart_id,
            "title": self.title,
            "note": self.note,
        }


def wants_category_pie(message: str) -> bool:
    text = message or ""
    has_category = any(marker in text for marker in ("品类", "分类"))
    has_pie = any(marker in text for marker in _PIE_MARKERS)
    return has_category and has_pie


def chart_role(question: str, message: str = "") -> ChartRole:
    """Tag the query the planner already wrote. The model does not emit this field."""
    text = question or ""
    folded = text.casefold()
    if any(marker in text or marker in folded for marker in _CATEGORY_MARKERS):
        return "pie_category"
    if any(marker in text for marker in _TREND_MARKERS):
        return "trend"
    del message
    return "bar"


def period_label(period: str | None) -> str:
    if not period or "-" not in period:
        return ""
    year, month = period.split("-", 1)
    try:
        return f"{int(year)}年{int(month)}月"
    except ValueError:
        return ""


def pie_category_question(message: str) -> str:
    period = period_from_message(message)
    when = period_label(period) or "最近一个报告期"
    if uses_purchase_orders(message):
        return f"{when}各品类的采购金额和采购箱数分别是多少？"
    return f"{when}各品类已确认的销售额分别是多少？"


def decision_period(question: str, message: str) -> str | None:
    return period_from_message(question) or period_from_message(message)


def skip_reason(role: ChartRole, rows: list[dict[str, Any]], columns: list[str]) -> str | None:
    """Why this dataset should not go to the chart model. None means draw it."""
    if no_matching_records(rows) or not column_kinds(rows):
        return "no_data"
    if role == "pie_category" and category_column(rows, columns) is None:
        return "bad_dimension"
    if role == "trend" and time_column(rows, columns) is None:
        return "bad_dimension"
    return None


def skip_note(role: ChartRole, reason: str, period: str | None) -> str:
    label = period_label(period) or "该期间"
    if reason == "no_data":
        if role == "pie_category":
            return f"{label}没有匹配的记录，未生成品类饼图"
        return f"{label}没有匹配的记录，未生成图表"
    if reason == "bad_dimension":
        return "查询结果的维度对不上，未生成该图"
    return "未生成图表"


def chart_user_hint(role: ChartRole, message: str) -> str:
    if role == "pie_category":
        return "按这一次查询结果画一张品类饼图。扇区名称必须是查询结果里的品类，不要用产品名称。最多一张。"
    if role == "trend":
        return "按这一次查询结果画一张按期间的趋势图（折线或柱状）。横轴必须是查询结果里的期间。最多一张。"
    if wants_category_pie(message):
        return "按这一次查询结果画一张柱状或折线图。用户要的是品类饼图，这一组结果不要画成饼图。最多一张。"
    return "按这一次查询结果画一张图。最多一张。"


def category_column(rows: list[dict[str, Any]], columns: list[str]) -> str | None:
    names = _column_names(rows, columns)
    for name in names:
        folded = name.casefold()
        if any(marker in folded or marker in name for marker in _CATEGORY_MARKERS):
            return name
    kinds = {item["name"]: item["kind"] for item in column_kinds(rows)}
    text_cols = [
        name for name in names
        if kinds.get(name) == "text" and not _is_product_column(name)
    ]
    if len(text_cols) == 1:
        return text_cols[0]
    return None


def time_column(rows: list[dict[str, Any]], columns: list[str]) -> str | None:
    names = _column_names(rows, columns)
    kinds = {item["name"]: item["kind"] for item in column_kinds(rows)}
    for name in names:
        if kinds.get(name) == "time":
            return name
        values = [row.get(name) for row in rows if isinstance(row, dict) and row.get(name) is not None]
        if values and all(_looks_like_period(value) for value in values):
            return name
        if value_kind(name, values) == "time":
            return name
    return None


def validate_chart(
    chart: dict[str, Any], *, role: ChartRole, message: str,
    rows: list[dict[str, Any]], columns: list[str],
) -> str | None:
    """None when the drawing matches the bound rows; otherwise a drop reason."""
    kind = _chart_type(chart)
    allowed = _allowed_types(role, message)
    if kind not in allowed:
        return "bad_dimension"
    labels = _chart_labels(chart)
    if not labels:
        return "bad_dimension"
    if role == "pie_category":
        column = category_column(rows, columns)
        if column is None:
            return "bad_dimension"
        known = _labels_in_column(rows, column)
        if not known or not all(_covered(label, known) for label in labels):
            return "bad_dimension"
        return None
    if role == "trend":
        if not all(_looks_like_period(label) for label in labels):
            return "bad_dimension"
        column = time_column(rows, columns)
        known = _labels_in_column(rows, column) if column else set()
        if known and not all(_covered(label, known) for label in labels):
            return "bad_dimension"
        return None
    known = _all_text_labels(rows, columns)
    if known and not all(_covered(label, known) for label in labels):
        return "bad_dimension"
    return None


def _allowed_types(role: ChartRole, message: str) -> set[str]:
    if role == "pie_category":
        return {"pie"}
    if role == "trend":
        return {"line", "bar"}
    allowed = {"bar", "horizontal_bar", "line"}
    if not wants_category_pie(message):
        allowed.add("pie")
    return allowed


def _chart_type(chart: dict[str, Any]) -> str:
    raw = str(chart.get("type") or "").strip().casefold().replace("-", "_")
    if raw in {"horizontalbar", "horizontal_bar"}:
        return "horizontal_bar"
    option = chart.get("echarts_option") if isinstance(chart.get("echarts_option"), dict) else {}
    series = option.get("series") if isinstance(option, dict) else None
    first = series[0] if isinstance(series, list) and series else series
    if not raw and isinstance(first, dict):
        raw = str(first.get("type") or "").strip().casefold().replace("-", "_")
        if raw in {"horizontalbar", "horizontal_bar"}:
            return "horizontal_bar"
    return raw


def _chart_labels(chart: dict[str, Any]) -> list[str]:
    option = chart.get("echarts_option") if isinstance(chart.get("echarts_option"), dict) else {}
    names: list[str] = []
    series = option.get("series") or []
    items = series if isinstance(series, list) else [series] if isinstance(series, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        data = item.get("data") or []
        if not isinstance(data, list):
            continue
        for point in data:
            if isinstance(point, dict) and point.get("name") not in (None, ""):
                names.append(str(point["name"]).strip())
    for axis_key in ("xAxis", "yAxis"):
        axis = option.get(axis_key)
        axes = axis if isinstance(axis, list) else [axis] if isinstance(axis, dict) else []
        for ax in axes:
            if not isinstance(ax, dict):
                continue
            data = ax.get("data")
            if not isinstance(data, list):
                continue
            for value in data:
                if value is None or isinstance(value, (int, float, dict, bool)):
                    continue
                text = str(value).strip()
                if text:
                    names.append(text)
    return names


def _column_names(rows: list[dict[str, Any]], columns: list[str]) -> list[str]:
    names = [str(name) for name in columns or [] if str(name)]
    if names:
        return names
    seen: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in seen:
                seen.append(text)
    return seen


def _is_product_column(name: str) -> bool:
    folded = name.casefold().replace(" ", "_")
    if any(marker in folded or marker in name for marker in _CATEGORY_MARKERS):
        return False
    return folded in _PRODUCT_COLS or name in _PRODUCT_COLS or folded.endswith("_sku")


def _labels_in_column(rows: list[dict[str, Any]], name: str) -> set[str]:
    labels: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(name)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text:
            labels.add(text)
    return labels


def _all_text_labels(rows: list[dict[str, Any]], columns: list[str]) -> set[str]:
    labels: set[str] = set()
    for name in _column_names(rows, columns):
        labels.update(_labels_in_column(rows, name))
    return labels


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold()


def _covered(label: str, known: set[str]) -> bool:
    needle = _norm(label)
    if not needle:
        return False
    for item in known:
        hay = _norm(item)
        if needle == hay or needle in hay or hay in needle:
            return True
    return False


def _looks_like_period(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    compact = text.replace(" ", "")
    return bool(_PERIOD_AXIS.match(compact))
