"""Failures worth showing on the run list.

A chat run stays `succeeded` when the reply was written. A failed tool or MCP call is a
warning on that child and on its owner. The run's own `error_json` is an error.
The list does not change the run status.
"""

from __future__ import annotations

from typing import Any

_MESSAGE_LIMIT = 300


def attach_run_alerts(runs: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy each run and set `alerts` from its own error and the tool rows under it."""
    listed = {str(run.get("id") or "") for run in runs}
    grouped: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in listed}
    for run in runs:
        alert = _run_error_alert(run)
        if alert is not None:
            grouped[str(run["id"])].append(alert)
    for call in calls:
        alert = _tool_alert(call)
        if alert is None:
            continue
        run_id = str(call.get("run_id") or "")
        owner_id = str(call.get("owner_run_id") or "")
        if run_id in grouped:
            grouped[run_id].append(alert)
        if owner_id in grouped and owner_id != run_id:
            grouped[owner_id].append(dict(alert))
    return [{**run, "alerts": _dedupe(grouped[str(run.get("id") or "")])} for run in runs]


def _run_error_alert(run: dict[str, Any]) -> dict[str, Any] | None:
    error = run.get("error_json") if isinstance(run.get("error_json"), dict) else {}
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    if not code and not message:
        return None
    return {
        "code": code or "RUN_FAILED",
        "message": (message or code)[:_MESSAGE_LIMIT],
        "tool_name": None,
        "run_id": str(run.get("id") or ""),
        "level": "error",
    }


def _tool_alert(call: dict[str, Any]) -> dict[str, Any] | None:
    """A tool or MCP call that failed. The child run can still be succeeded."""
    error = call.get("error_json") if isinstance(call.get("error_json"), dict) else {}
    result = call.get("result_json") if isinstance(call.get("result_json"), dict) else {}
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    status = str(call.get("status") or "")
    if status == "failed" or code or message:
        return _alert(code or "TOOL_FAILED", message or "tool failed", call)
    text = _result_text(result)
    if result.get("ok") is False or text.lower().startswith("mcp error"):
        validation = "32602" in text or "invalid arguments" in text.lower() or "validation" in text.lower()
        return _alert(
            "TOOL_VALIDATION_ERROR" if validation else "TOOL_UPSTREAM_ERROR",
            text or str(result.get("error") or "tool failed"),
            call,
        )
    return None


def _result_text(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        return str(data.get("text") or "").strip()
    if isinstance(data, str):
        return data.strip()
    return ""


def _alert(code: str, message: str, call: dict[str, Any]) -> dict[str, Any]:
    tool_name = call.get("tool_name")
    return {
        "code": code,
        "message": message[:_MESSAGE_LIMIT],
        "tool_name": tool_name if isinstance(tool_name, str) and tool_name else None,
        "run_id": str(call.get("run_id") or ""),
        "level": "warning",
    }


def _dedupe(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    kept: list[dict[str, Any]] = []
    for alert in alerts:
        key = (str(alert.get("code") or ""), str(alert.get("tool_name") or ""), str(alert.get("run_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        kept.append(alert)
    return kept
