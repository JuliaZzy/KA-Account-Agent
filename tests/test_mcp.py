"""MCP config loading and OpenAI schema conversion — no live SocialCrawl process."""

from __future__ import annotations

from types import SimpleNamespace

from backend.mcp.runtime import (
    _expand,
    _expand_env_map,
    load_server_configs,
    mcp_call_result,
    mcp_tool_to_openai_schema,
)


def test_servers_json_lists_socialcrawl_http():
    cfg = load_server_configs()
    assert "socialcrawl" in cfg
    assert cfg["socialcrawl"]["transport"] == "http"
    assert cfg["socialcrawl"]["url"] == "https://mcp.socialcrawl.dev/mcp"
    assert "Authorization" in (cfg["socialcrawl"].get("headers") or {})


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "sc_test")
    assert _expand("${SOCIALCRAWL_API_KEY}") == "sc_test"
    assert _expand_env_map({"SOCIALCRAWL_API_KEY": "${SOCIALCRAWL_API_KEY}"})["SOCIALCRAWL_API_KEY"] == "sc_test"
    assert _expand_env_map(["SOCIALCRAWL_API_KEY"])["SOCIALCRAWL_API_KEY"] == "sc_test"


def test_mcp_tool_to_openai_schema():
    tool = SimpleNamespace(
        name="socialcrawl_request",
        description="Call any SocialCrawl endpoint",
        inputSchema={
            "type": "object",
            "properties": {"platform": {"type": "string"}, "endpoint": {"type": "string"}},
            "required": ["platform", "endpoint"],
        },
    )
    schema = mcp_tool_to_openai_schema(tool)
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "socialcrawl_request"
    assert schema["function"]["parameters"]["required"] == ["platform", "endpoint"]


def test_mcp_validation_text_is_a_failed_call():
    text = (
        'MCP error -32602: Input validation error: Invalid arguments for tool socialcrawl_find: '
        'Unknown key "message". Allowed keys: task, platform, limit.'
    )
    result = mcp_call_result(text, is_error=False)
    assert result["ok"] is False
    assert result["code"] == "TOOL_VALIDATION_ERROR"
    assert "task" in result["error"]
    assert mcp_call_result("MCP error -32000: upstream down", is_error=False)["code"] == "TOOL_UPSTREAM_ERROR"
    assert mcp_call_result('{"title": "ok"}', is_error=False) == {
        "ok": True, "data": {"title": "ok"}, "error": None,
    }
