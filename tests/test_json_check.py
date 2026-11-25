"""JSON replies are sent back when the object is unparseable or missing required fields."""

from __future__ import annotations

import asyncio

import pytest

from backend.llm import calls
from backend.llm.calls import JsonRepairExhausted, ModelOutputError
from backend.llm.json_check import (
    JSON_FAILURE_MESSAGE,
    JSON_REPAIRS,
    inspect_json,
    repair_json,
    repair_json_async,
)
from backend.po.extract import EXTRACT_SCHEMA


def test_inspect_reports_missing_and_nested_fields():
    issues = inspect_json({"line_items": [{"cases": 1}]}, EXTRACT_SCHEMA)
    text = " ".join(issue.message for issue in issues)
    assert "缺少 reply_language" in text
    assert "缺少 line_items[0].reference" in text
    assert "缺少 line_items[0].sku" in text


def test_a_missing_field_is_sent_back_once():
    seen: list[list[dict]] = []

    def fetch(messages):
        seen.append(messages)
        if len(seen) == 1:
            return {"line_items": []}
        return {"line_items": [], "reply_language": "zh"}

    raw = repair_json(
        fetch, messages=[{"role": "user", "content": "两箱"}],
        schema=EXTRACT_SCHEMA, purpose="po.extract",
    )
    assert raw["reply_language"] == "zh"
    assert "缺少 reply_language" in seen[1][-1]["content"]
    assert "上一次输出" in seen[1][-1]["content"]


def test_unparseable_json_is_sent_back_with_the_raw_text():
    seen: list[list[dict]] = []

    def fetch(messages):
        seen.append(messages)
        if len(seen) == 1:
            raise ModelOutputError("model did not return valid JSON", raw="```json\n{")
        return {"line_items": [], "reply_language": "zh"}

    raw = repair_json(
        fetch, messages=[{"role": "user", "content": "两箱"}],
        schema=EXTRACT_SCHEMA, purpose="po.extract",
    )
    assert raw["reply_language"] == "zh"
    assert "不是合法 JSON" in seen[1][-1]["content"]
    assert "```json" in seen[1][-2]["content"]


def test_a_cut_off_incomplete_object_asks_for_a_shorter_one():
    seen: list[list[dict]] = []

    def fetch(messages):
        seen.append(messages)
        calls._FINISH.set("length")
        if len(seen) == 1:
            return {"line_items": []}
        calls._FINISH.set(None)
        return {"line_items": [], "reply_language": "zh"}

    raw = repair_json(
        fetch, messages=[{"role": "user", "content": "两箱"}],
        schema=EXTRACT_SCHEMA, purpose="po.extract",
    )
    assert raw["reply_language"] == "zh"
    assert "截断" in seen[1][-1]["content"]


def test_a_complete_object_is_kept_when_the_vendor_stopped_for_length():
    seen: list[int] = []

    def fetch(messages):
        del messages
        seen.append(1)
        calls._FINISH.set("length")
        return {"line_items": [], "reply_language": "zh"}

    raw = repair_json(
        fetch, messages=[{"role": "user", "content": "两箱"}],
        schema=EXTRACT_SCHEMA, purpose="po.extract",
    )
    assert raw["reply_language"] == "zh"
    assert seen == [1]


def test_a_schema_miss_after_the_repair_budget_is_not_returned():
    seen: list[int] = []

    def fetch(messages):
        del messages
        seen.append(1)
        return {"line_items": []}

    with pytest.raises(JsonRepairExhausted) as caught:
        repair_json(
            fetch, messages=[{"role": "user", "content": "两箱"}],
            schema=EXTRACT_SCHEMA, purpose="po.extract",
        )
    assert str(caught.value) == JSON_FAILURE_MESSAGE
    assert caught.value.code == "JSON_REPAIR_EXHAUSTED"
    assert len(seen) == JSON_REPAIRS + 1


def test_unparseable_json_after_the_repair_budget_fails_the_same_way():
    async def fetch(messages):
        del messages
        raise ModelOutputError("model did not return valid JSON", raw="{")

    with pytest.raises(JsonRepairExhausted) as caught:
        asyncio.run(repair_json_async(
            fetch, messages=[{"role": "user", "content": "两箱"}],
            schema=EXTRACT_SCHEMA, purpose="po.extract",
        ))
    assert str(caught.value) == JSON_FAILURE_MESSAGE


def test_data_and_brand_do_not_use_the_repair_loop():
    for purpose in ("data.react", "brand.query", "orchestrator.plan"):
        seen: list[int] = []

        def fetch(messages, _seen=seen):
            del messages
            _seen.append(1)
            return {"not": "the schema"}

        assert repair_json(
            fetch, messages=[{"role": "user", "content": "hi"}],
            schema=EXTRACT_SCHEMA, purpose=purpose,
        ) == {"not": "the schema"}
        assert seen == [1]
