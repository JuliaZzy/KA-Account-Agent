"""Domain modules register as tools. They return dicts."""

from __future__ import annotations

import asyncio

from backend.llm import FakeLLM
from backend.service import new_session
from backend.services.session_tools import tools


def test_contract_read_without_a_file_asks_for_one(db):
    db.upsert_user("u-a", "dist-1", "Ada")
    session = new_session(db, "ps-1", "u-a")
    registry = tools(db, FakeLLM(), session, "请读合同")
    result = asyncio.run(registry.get("contract.read").fn({}))
    assert result["status"] == "need_file"
    assert result["table"] is None