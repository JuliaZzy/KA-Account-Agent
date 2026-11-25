"""Session open — title and tags for a new chat."""

from __future__ import annotations

import asyncio

from backend.llm import FakeLLM
from backend.orchestrator import TOPIC_KEYS, open_session


def test_open_session_returns_title_and_tags():
    llm = FakeLLM(json_replies=[{"tags": ["po"], "title": "20 cases fish oil"}])
    opened = asyncio.run(open_session(llm, message="20 cases of fish oil", attachments=[]))
    assert opened.tags == ["po"] and opened.title == "20 cases fish oil"
    assert set(llm.calls[0]["schema"]["schema"]["properties"]["tags"]["items"]["enum"]) == set(TOPIC_KEYS)


def test_open_session_drops_unknown_tags_and_defaults_title():
    llm = FakeLLM(json_replies=[{"tags": ["po", "made_up", "data"], "title": "  "}])
    opened = asyncio.run(open_session(llm, message="", attachments=["deal.pdf"]))
    assert opened.tags == ["po", "data"] and opened.title == "deal.pdf"


def test_open_session_does_not_send_history():
    llm = FakeLLM(json_replies=[{"tags": [], "title": "hello"}])
    asyncio.run(open_session(llm, message="hi", attachments=[]))
    user = llm.calls[0]["messages"][0]["content"]
    assert "最近对话" not in user
    assert "=== 消息 ===" in user
