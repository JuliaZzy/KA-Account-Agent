"""从查询行推导出的数字。模型引用这些；它不重算。

每个数字列一条发现。时间列加数字是趋势（首期、末期、变化）。短文本列加数字是结构（合计和占比）。
两者都没有的数字是范围（最小、最大、均值）。单独一个值留在行里。
占比四舍五入到一位小数。均值四舍五入到两位。

风险标记也在这里算：可售天数、环比降幅、逾期采购单、缺合同目标。阈值写在本文件里。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

MAX_GROUPS = 12
MAX_METRICS = 2
LOW_COVER_DAYS = 30
SALES_DROP_PERCENT = 20.0
MAX_RISK_FLAGS = 8
_PO_DONE = frozenset({"completed", "cancelled", "canceled"})
_TIME_TOKENS = frozenset({"year", "month", "quarter", "week", "date", "period", "day"})
_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([T ].*)?$")


def findings(query_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trend, mix, or range for up to two numeric columns. Nothing when the rows cannot support one."""
    if not rows:
        return []
    names = _names(rows)
    numeric = [name for name in names if value_kind(name, _values(rows, name)) == "number" and not _id_like(name)]
    time_name = next((name for name in names if value_kind(name, _values(rows, name)) == "time"), None)
    text_name = _mix_column(rows, [name for name in names if value_kind(name, _values(rows, name)) == "text"])
    found: list[dict[str, Any]] = []
    for name in numeric[:MAX_METRICS]:
        emitted = False
        if time_name is not None:
            trend = _trend(query_id, rows, time_name, name)
            if trend is not None:
                found.append(trend)
                emitted = True
        if text_name is not None:
            mix = _mix(query_id, rows, text_name, name)
            if mix is not None:
                found.append(mix)
                emitted = True
        if emitted:
            continue
        span = _range(query_id, rows, name)
        if span is not None:
            found.append(span)
    return found


def value_kind(name: str, values: list[Any]) -> str:
    """``time``, ``number``, or ``text``. A year column stays time even though its cells are integers."""
    present = [value for value in values if value is not None and value != ""]
    if not present:
        return "text"
    if _is_time_name(name) or all(_is_date(value) for value in present):
        return "time"
    if all(_is_number(value) for value in present):
        return "number"
    return "text"


def column_kinds(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Column name and kind for the chart prompt. The cells themselves stay out."""
    return [
        {"name": name, "kind": value_kind(name, _values(rows, name))}
        for name in _names(rows)[:12]
    ]


def _names(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        for key in row:
            if key not in names:
                names.append(str(key))
    return names


def _values(rows: list[dict[str, Any]], name: str) -> list[Any]:
    return [row.get(name) for row in rows if row.get(name) is not None]


def _id_like(name: str) -> bool:
    folded = name.casefold()
    return folded == "id" or folded.endswith("_id")


def _is_time_name(name: str) -> bool:
    tokens = set(name.casefold().replace("-", "_").split("_"))
    return bool(tokens & _TIME_TOKENS)


def _is_date(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATE.match(value.strip()))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _label(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value == value and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _number(value: Any) -> int | float | None:
    if not _is_number(value):
        return None
    return value


def _mix_column(rows: list[dict[str, Any]], names: list[str]) -> str | None:
    """The text column with the fewest distinct labels, when at least two labels repeat or fit on one chart."""
    best: str | None = None
    best_count: int | None = None
    for name in names:
        labels = [_label(row.get(name)) for row in rows]
        labels = [label for label in labels if label]
        distinct = len(set(labels))
        if distinct < 2:
            continue
        if distinct == len(labels) and distinct > MAX_GROUPS:
            continue
        if best_count is None or distinct < best_count:
            best, best_count = name, distinct
    return best


def _buckets(rows: list[dict[str, Any]], x_name: str, y_name: str) -> list[tuple[str, int | float]]:
    grouped: dict[str, int | float] = {}
    order: list[str] = []
    for row in rows:
        label = _label(row.get(x_name))
        value = _number(row.get(y_name))
        if not label or value is None:
            continue
        if label not in grouped:
            order.append(label)
            grouped[label] = value
        else:
            grouped[label] = grouped[label] + value
    return [(label, grouped[label]) for label in order]


def _trend(query_id: str, rows: list[dict[str, Any]], x_name: str, y_name: str) -> dict[str, Any] | None:
    pairs = sorted(_buckets(rows, x_name, y_name), key=lambda item: item[0])
    if len(pairs) < 2:
        return None
    start_label, start_value = pairs[0]
    end_label, end_value = pairs[-1]
    change = end_value - start_value
    found: dict[str, Any] = {
        "id": query_id, "kind": "trend", "x": x_name, "y": y_name,
        "start": {"label": start_label, "value": start_value},
        "end": {"label": end_label, "value": end_value},
        "change": change,
        "direction": "up" if change > 0 else "down" if change < 0 else "flat",
    }
    if start_value != 0:
        found["change_percent"] = round(change / start_value * 100, 1)
    return found


def _mix(query_id: str, rows: list[dict[str, Any]], x_name: str, y_name: str) -> dict[str, Any] | None:
    pairs = _buckets(rows, x_name, y_name)
    if len(pairs) < 2:
        return None
    total = sum(value for _, value in pairs)
    shares = total > 0 and all(value >= 0 for _, value in pairs)
    groups = []
    for label, value in sorted(pairs, key=lambda item: (-item[1], item[0])):
        group: dict[str, Any] = {"label": label, "value": value}
        if shares:
            group["share_percent"] = round(value / total * 100, 1)
        groups.append(group)
    return {
        "id": query_id, "kind": "mix", "x": x_name, "y": y_name,
        "total": total, "group_count": len(groups),
        "top": groups[0], "bottom": min(groups, key=lambda group: (group["value"], group["label"])),
        "groups": groups[:MAX_GROUPS],
    }


def cover_days(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Days of cover for a SKU that has both ending stock and period units.

    ``stock / units * 30``. Thirty days is the reporting month. This is not a forecast.
    """
    stock_by_sku: dict[str, float] = {}
    units_by_sku: dict[str, float] = {}
    name_by_sku: dict[str, str] = {}
    for rows in groups:
        for row in rows:
            if not isinstance(row, dict):
                continue
            sku = str(row.get("sku") or "").strip()
            if not sku:
                continue
            product = _product_name(row)
            if product:
                name_by_sku[sku] = product
            for key, value in row.items():
                number = _number(value)
                if number is None:
                    continue
                label = str(key)
                if _stock_column(label):
                    stock_by_sku[sku] = float(number)
                elif _units_column(label):
                    units_by_sku[sku] = float(number)
    notes: list[dict[str, Any]] = []
    for sku in sorted(set(stock_by_sku) & set(units_by_sku)):
        stock = stock_by_sku[sku]
        units = units_by_sku[sku]
        if units <= 0 or stock < 0:
            continue
        note: dict[str, Any] = {
            "sku": sku, "stock": stock, "units": units,
            "cover_days": round(stock / units * 30, 1),
        }
        if sku in name_by_sku:
            note["product_name"] = name_by_sku[sku]
        notes.append(note)
    return notes


def risk_flags(
    datasets: list[dict[str, Any]], *, today: date | None = None,
) -> list[dict[str, Any]]:
    """Thresholded markers from already-fetched rows. Empty means skip the chapter."""
    today = today or date.today()
    rows_groups = [list(item.get("rows") or []) for item in datasets if isinstance(item, dict)]
    flags: list[dict[str, Any]] = []
    flags.extend(_missing_target_flags(datasets))
    flags.extend(_overdue_po_flags(datasets, today))
    flags.extend(_low_cover_flags(rows_groups))
    flags.extend(_sales_drop_flags(datasets))
    return flags[:MAX_RISK_FLAGS]


def _low_cover_flags(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for note in sorted(cover_days(groups), key=lambda item: item["cover_days"]):
        days = float(note["cover_days"])
        if days >= LOW_COVER_DAYS:
            continue
        sku = str(note["sku"])
        product = str(note.get("product_name") or "").strip()
        label = f"{product}（{sku}）" if product else sku
        flags.append(_flag(
            "low_cover", "可售天数偏低",
            f"{label} 可售 {days} 天，低于 {LOW_COVER_DAYS} 天。",
            {**note, "threshold": LOW_COVER_DAYS},
        ))
    return flags


def _sales_drop_flags(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    ranked: list[tuple[float, dict[str, Any]]] = []
    for item in datasets:
        if not isinstance(item, dict):
            continue
        query_id = str(item.get("id") or "")
        for found in findings(query_id, list(item.get("rows") or [])):
            if found.get("kind") != "trend" or found.get("direction") != "down":
                continue
            if _stock_column(str(found.get("y") or "")):
                continue
            percent = found.get("change_percent")
            if not isinstance(percent, (int, float)) or percent > -SALES_DROP_PERCENT:
                continue
            ranked.append((float(percent), found))
    ranked.sort(key=lambda item: item[0])
    for percent, found in ranked:
        start = found.get("start") if isinstance(found.get("start"), dict) else {}
        end = found.get("end") if isinstance(found.get("end"), dict) else {}
        metric = str(found.get("y") or "指标")
        flags.append(_flag(
            "sales_drop", "环比下降",
            (
                f"{metric} 从 {start.get('label')} 的 {start.get('value')} "
                f"降到 {end.get('label')} 的 {end.get('value')}，降幅 {abs(percent)}%。"
            ),
            {**found, "threshold_percent": SALES_DROP_PERCENT},
        ))
    return flags


def _overdue_po_flags(datasets: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in datasets:
        if not isinstance(item, dict):
            continue
        for row in item.get("rows") or []:
            if not isinstance(row, dict):
                continue
            eta = _row_date(row, _eta_column)
            status = _row_text(row, _status_column)
            if eta is None or eta >= today or not _po_open(status):
                continue
            number = _row_text(row, _po_number_column) or str(row.get("id") or "").strip()
            key = number or json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            label = number or "采购单"
            amount = _row_number(row, _amount_column)
            evidence: dict[str, Any] = {
                "po_number": number, "status": status, "eta_date": eta.isoformat(),
            }
            if amount is not None:
                evidence["amount"] = amount
            flags.append(_flag(
                "overdue_po", "采购单逾期",
                f"{label} 预计 {eta.isoformat()} 到达，状态 {status or '未知'}，已经逾期。",
                evidence,
            ))
    return flags


def _missing_target_flags(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    saw_target_column = False
    for item in datasets:
        if not isinstance(item, dict):
            continue
        rows = [row for row in (item.get("rows") or []) if isinstance(row, dict)]
        if not rows:
            continue
        columns = [str(name) for name in (item.get("columns") or _names(rows))]
        target_col = next((name for name in columns if _target_column(name)), None)
        if target_col is None:
            continue
        saw_target_column = True
        if any(_target_amount(row.get(target_col)) is not None for row in rows):
            return []
    if not saw_target_column:
        return []
    return [_flag(
        "missing_target", "缺合同目标",
        "已查到的合同没有可用的年度销售目标。",
        {"threshold": "annual_sales_target"},
    )]


def _flag(kind: str, title: str, detail: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "title": title, "detail": detail, "evidence": evidence}


def _product_name(row: dict[str, Any]) -> str:
    for key in ("product_name", "product_name_native", "product"):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def _row_text(row: dict[str, Any], match) -> str:
    for key, value in row.items():
        if match(str(key)):
            return str(value or "").strip()
    return ""


def _row_number(row: dict[str, Any], match) -> int | float | None:
    for key, value in row.items():
        if match(str(key)):
            return _number(value)
    return None


def _row_date(row: dict[str, Any], match) -> date | None:
    for key, value in row.items():
        if match(str(key)):
            parsed = _as_date(value)
            if parsed is not None:
                return parsed
    return None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not _DATE.match(text):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _po_open(status: str) -> bool:
    text = status.strip().casefold()
    return bool(text) and text not in _PO_DONE


def _eta_column(name: str) -> bool:
    folded = name.casefold().replace("-", "_")
    return folded in {"eta_date", "eta"} or "eta" in folded.split("_")


def _status_column(name: str) -> bool:
    return name.casefold() == "status"


def _po_number_column(name: str) -> bool:
    folded = name.casefold().replace("-", "_")
    return folded in {"po_number", "order_number"} or "po_number" in folded


def _amount_column(name: str) -> bool:
    folded = name.casefold().replace("-", "_")
    return folded in {"total_amount", "amount", "line_total"}


def _target_column(name: str) -> bool:
    return "annual_sales_target" in name.casefold() and "currency" not in name.casefold()


def _target_amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() in {"null", "none"}:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return _number(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "target"):
            number = _number(value.get(key))
            if number is not None:
                return float(number)
        return None
    number = _number(value)
    return None if number is None else float(number)


def _stock_column(name: str) -> bool:
    folded = name.casefold()
    if "库存" in name:
        return True
    return any(token in folded for token in ("stock", "inventory", "on_hand"))


def _units_column(name: str) -> bool:
    if _stock_column(name):
        return False
    if "销量" in name:
        return True
    folded = name.casefold()
    return "units" in folded or folded in {"qty", "quantity"}


def _range(query_id: str, rows: list[dict[str, Any]], y_name: str) -> dict[str, Any] | None:
    values = [value for value in (_number(row.get(y_name)) for row in rows) if value is not None]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    shown: int | float = int(mean) if mean == int(mean) else round(mean, 2)
    return {
        "id": query_id, "kind": "range", "y": y_name,
        "count": len(values), "min": min(values), "max": max(values), "mean": shown,
    }
