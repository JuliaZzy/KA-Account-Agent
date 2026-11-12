"""Each prompts module exposes string constants; fill() substitutes slots without touching JSON braces."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from backend.orchestrator.prompt import OPEN
from backend.po.extract import build_extract_prompt
from backend.po.prompt import EXTRACT, fill
from backend.po.slots import PoSlots
from backend.report import prompt as report
from backend.text2sql import prompt as data_query

ROOT = Path(__file__).resolve().parents[1] / "backend"


def test_registry_modules_import_and_have_prompt_strings():
    registry = json.loads((ROOT / "orchestrator" / "registry.json").read_text(encoding="utf-8"))
    for agent in registry["agents"]:
        mod = importlib.import_module(agent["module"])
        strings = [v for k, v in vars(mod).items() if k.isupper() and isinstance(v, str)]
        assert strings, agent["id"]
    mcp = json.loads((ROOT / "mcp" / "servers.json").read_text(encoding="utf-8"))
    assert set(mcp["mcpServers"]) == {"socialcrawl"}
    ids = {a["id"] for a in registry["agents"]}
    assert {"po", "contract", "report", "data", "session_open"} <= ids


def test_fill_leaves_json_braces_and_fills_slots():
    text = fill(EXTRACT, image_block="", context_block="", manifest="FO-100", slots='{"line_items": []}', contract_discount="")
    assert "FO-100" in text and "{{manifest}}" not in text
    assert '{"line_items"' in text


def test_build_extract_prompt_uses_the_po_constants(catalog):
    prompt = build_extract_prompt(catalog.manifest(), PoSlots(), has_images=True, operating_context="Ship on Friday.")
    assert "附件图片" in prompt and "Ship on Friday" in prompt and "FO-100" in prompt
    assert OPEN.startswith("你在为一场品牌与已入驻经销商的新对话做开场标记")
    assert "PLACEHOLDER" not in data_query.REACT
    assert all(action in data_query.REACT for action in ("inspect_schema", "run_sql", "finish"))
    assert "PLACEHOLDER" not in report.REPORT_QUESTIONS_PROMPT and '"questions"' in report.REPORT_QUESTIONS_PROMPT
    assert '"charts"' in report.CHART_GENERATION_PROMPT and not hasattr(report, "REFLECT")
