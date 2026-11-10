"""Tools return a typed envelope. Translate uses the official Qwen path; search does not hit Bocha."""

from __future__ import annotations

import json

import pytest

from backend.llm import AssistantTurn, FakeLLM, ToolCall
from backend.orchestrator import run_tool_calls
from backend.tools import SCHEMAS, TOOLS
from backend.tools.exchange_rate import quote
from backend.tools.exchange_rate import run as fx_run
from backend.tools.lang_translate import run as translate_run
from backend.tools.send_email import MailError
from backend.tools.send_email import run as email_run
from backend.tools.send_email import send as email_send
from backend.tools.web_search import run as search_run
from backend.tools.web_search import search


class _School:
    def __init__(self, text: str):
        self.calls: list[dict] = []
        self.text = text

    def chat_completion(self, model, messages, stream=False, max_tokens=None, temperature=None):
        self.calls.append({"model": model, "messages": messages, "temperature": temperature})
        return {"choices": [{"message": {"content": self.text}}]}


def _school(monkeypatch, text: str) -> _School:
    client = _School(text)
    monkeypatch.setattr("backend.llm.client.qwen_client", lambda: client)
    return client


def test_translate_returns_only_the_model_text(monkeypatch):
    school = _school(monkeypatch, "ten cases of fish oil")
    out = translate_run({"text": "10 箱鱼油", "target_language": "English"}, llm=FakeLLM())
    assert out == {"ok": True, "data": {"translation": "ten cases of fish oil"}, "error": None}
    assert school.calls[0]["model"] == "qwen3.7-flash"
    assert "10 箱鱼油" in school.calls[0]["messages"][1]["content"]


def test_translate_accepts_target_as_the_language(monkeypatch):
    school = _school(monkeypatch, "Hello")
    out = translate_run({"text": "你好", "target": "en"}, llm=FakeLLM())
    assert out["ok"] is True and out["data"]["translation"] == "Hello"
    assert "目标语言：en" in school.calls[0]["messages"][1]["content"]


def test_translate_rejects_an_empty_request():
    assert translate_run({"text": "  "}, llm=FakeLLM())["ok"] is False
    assert translate_run({"target_language": "English", "source": "message"}, llm=FakeLLM())["error"] == "text required when source is message"


def test_translate_uses_the_last_assistant_turn_when_asked(monkeypatch):
    school = _school(monkeypatch, "Order placed.")
    ctx = {"history": [
        {"role": "user", "content": "10 cases"},
        {"role": "assistant", "content": "已下单。"},
    ]}
    out = translate_run({"target_language": "English", "source": "last_assistant"}, llm=FakeLLM(), context=ctx)
    assert out["ok"] and "已下单。" in school.calls[0]["messages"][1]["content"]


def test_translate_uses_the_session_report_and_fails_if_missing(monkeypatch):
    school = _school(monkeypatch, "Sales were flat.")
    out = translate_run(
        {"target_language": "English", "source": "report"},
        llm=FakeLLM(),
        context={"last_report": "本月销量持平。"},
    )
    assert out["ok"] and "本月销量持平。" in school.calls[0]["messages"][1]["content"]
    assert translate_run({"target_language": "English", "source": "report"}, llm=FakeLLM(), context={})["ok"] is False


def test_web_search_parses_bocha_pages(monkeypatch):
    payload = {
        "data": {"webPages": {"value": [
            {"name": "Nordic Beauty", "snippet": "Distributor in Stockholm", "url": "https://example.com"},
        ]}},
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    rows = search("Nordic Beauty")
    assert rows == [{"title": "Nordic Beauty", "snippet": "Distributor in Stockholm", "url": "https://example.com"}]
    assert search_run({"query": "Nordic Beauty"})["ok"] is True


def test_orchestrator_runs_translate_then_answers(monkeypatch):
    school = _school(monkeypatch, "ten cases of fish oil")
    llm = FakeLLM(
        tool_replies=[
            AssistantTurn(tool_calls=[ToolCall(
                id="call_1", name="translate",
                arguments={"text": "10 箱鱼油", "target_language": "English"},
            )]),
            AssistantTurn(content="ten cases of fish oil"),
        ],
    )
    reply = run_tool_calls(llm, text="把这句话译成英语：10 箱鱼油", history=[], include_mcp=False)
    assert reply == "ten cases of fish oil"
    assert [c["kind"] for c in llm.calls] == ["complete", "complete"]
    assert llm.calls[0]["tools"] == SCHEMAS
    assert school.calls[0]["model"] == "qwen3.7-flash"
    assert "10 箱鱼油" in school.calls[0]["messages"][1]["content"]
    tool_turn = llm.calls[1]["messages"][-1]
    assert tool_turn["role"] == "tool"
    assert "ten cases of fish oil" in tool_turn["content"]


def test_orchestrator_skips_tools_when_the_model_calls_none():
    assert run_tool_calls(FakeLLM(), text="10 cases of fish oil", history=[], include_mcp=False) is None


def test_web_search_query_borrows_the_previous_topic():
    from backend.services.task_run import _arguments_for

    history = [
        {"role": "user", "content": "那改成查上海的天气吧"},
        {"role": "assistant", "content": "需要我用 web_search 吗"},
    ]
    echoed = _arguments_for(
        {"name": "web_search", "message": "可以，用web_search吧", "input": {"query": "可以，用web_search吧"}},
        history,
    )
    assert echoed["query"] == "那改成查上海的天气吧"
    empty = _arguments_for(
        {"name": "web_search", "message": "可以，用web_search吧", "input": {}},
        history,
    )
    assert empty["query"] == "那改成查上海的天气吧"
    planned = _arguments_for(
        {"name": "web_search", "message": "可以，用web_search吧", "input": {"query": "上海近期天气"}},
        history,
    )
    assert planned["query"] == "上海近期天气"
    named = _arguments_for(
        {"name": "web_search", "message": "查上海天气", "input": {"query": "查上海天气"}},
        [],
    )
    assert named["query"] == "查上海天气"


def test_tools_registry_names():
    names = [s["function"]["name"] for s in SCHEMAS]
    assert names == list(TOOLS)
    assert set(TOOLS) == {"web_search", "translate", "exchange_rate", "send_email"}


def test_exchange_rate_normalizes_rmb_and_quotes(monkeypatch):
    from backend.redis_store import fx_drop

    fx_drop("CNY", "EUR")
    payload = {"amount": 1.0, "base": "CNY", "date": "2026-10-14", "rates": {"EUR": 0.13}}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    opened: list[object] = []

    def _open(req, timeout=8):
        del timeout
        opened.append(req)
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _open)
    data = quote("rmb", "eur")
    sent = opened[0]
    assert "api.frankfurter.dev" in sent.full_url
    assert sent.get_header("User-agent") == "KA_Account/1.0"
    assert data["base"] == "CNY" and data["target"] == "EUR" and data["converted"] == 0.13
    assert fx_run({"base_currency": "CNY", "target_currency": "EUR"})["ok"] is True
    assert fx_run({"base_currency": "", "target_currency": "EUR"})["ok"] is False


def test_send_email_dry_run_and_validation(monkeypatch):
    monkeypatch.setenv("EMAIL_DRY_RUN", "1")
    out = email_run({
        "to": "buyer@example.com",
        "subject": "Quote",
        "body": "10 cases fish oil",
    })
    assert out["ok"] and out["data"]["dry_run"] is True and out["data"]["to"] == "buyer@example.com"
    with pytest.raises(MailError) as bad_address:
        email_run({"to": "not-an-email", "subject": "x", "body": "y"})
    assert bad_address.value.code == "TOOL_VALIDATION_ERROR"
    with pytest.raises(MailError) as empty_subject:
        email_run({"to": "a@b.com", "subject": "", "body": "y"})
    assert empty_subject.value.code == "TOOL_VALIDATION_ERROR"
    monkeypatch.delenv("EMAIL_DRY_RUN", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with pytest.raises(MailError) as missing_smtp:
        email_run({"to": "a@b.com", "subject": "x", "body": "y"})
    assert missing_smtp.value.code == "TOOL_UPSTREAM_ERROR"
    try:
        email_send(to="a@b.com", subject="x", body="y", dry_run=False)
        raise AssertionError("expected SMTP_HOST error")
    except RuntimeError as err:
        assert "SMTP_HOST" in str(err)
