"""Run-list alerts stay off the run status. A finished chat can still carry a tool failure."""

from backend.services.run_alerts import attach_run_alerts


def test_child_tool_failure_alerts_the_chat_and_the_child():
    chat = {"id": "run-chat", "status": "succeeded", "error_json": None}
    child = {"id": "run-tool", "status": "succeeded", "error_json": None}
    calls = [{
        "run_id": "run-tool",
        "owner_run_id": "run-chat",
        "tool_name": "web_search",
        "status": "failed",
        "error_json": {
            "code": "PROVIDER_AUTHENTICATION",
            "message": "authentication HTTP 403 from bocha web-search",
        },
        "result_json": {},
    }]
    listed = attach_run_alerts([chat, child], calls)
    assert listed[0]["status"] == "succeeded"
    assert listed[0]["alerts"] == [{
        "code": "PROVIDER_AUTHENTICATION",
        "message": "authentication HTTP 403 from bocha web-search",
        "tool_name": "web_search",
        "run_id": "run-tool",
        "level": "warning",
    }]
    assert listed[1]["alerts"] == listed[0]["alerts"]


def test_mcp_text_on_a_succeeded_call_is_a_warning():
    chat = {"id": "run-chat", "status": "succeeded", "error_json": {}}
    calls = [{
        "run_id": "run-tool",
        "owner_run_id": "run-chat",
        "tool_name": "socialcrawl_find",
        "status": "succeeded",
        "error_json": {},
        "result_json": {"ok": True, "data": {"text": "MCP error -32602: Invalid arguments"}},
    }]
    listed = attach_run_alerts([chat], calls)
    alert = listed[0]["alerts"][0]
    assert alert["level"] == "warning"
    assert alert["code"] == "TOOL_VALIDATION_ERROR"
    assert alert["tool_name"] == "socialcrawl_find"


def test_run_error_json_is_its_own_alert():
    run = {
        "id": "run-failed",
        "status": "failed",
        "error_json": {"code": "TIMEOUT", "message": "timed out"},
    }
    listed = attach_run_alerts([run], [])
    assert listed[0]["alerts"] == [{
        "code": "TIMEOUT",
        "message": "timed out",
        "tool_name": None,
        "run_id": "run-failed",
        "level": "error",
    }]
