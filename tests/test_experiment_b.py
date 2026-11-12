"""Experiment B keeps OCR usage and distinguishes missing ledgers from zero cost."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import httpx

from docs.experiments.golden_set import run_golden, summarize_golden


def test_b_ledger_reads_ocr_usage_and_purpose(db, monkeypatch):
    from backend.documents import load_pages

    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")
    monkeypatch.setattr("backend.llm.client.GLMOCRClient.parse", lambda *_args: {
        "md_results": "Agreement", "usage": {"prompt_tokens": 40000, "completion_tokens": 10000},
    })
    run = db.create_inline_contract_run("dist-1", "u-a", "agreement.pdf")
    load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    db.transition_run("dist-1", run["id"], "succeeded")

    ledger, _ = run_golden.ledger(db, run["id"])
    assert ledger["model_calls"] == 1
    assert ledger["prompt_tokens"] == 40000 and ledger["completion_tokens"] == 10000
    assert ledger["cost_cny"] == 0.01 and ledger["unknown_usage_calls"] == 0
    assert ledger["calls"][0]["purpose"] == "document.ocr"


def test_b_summary_keeps_imports_separate_and_marks_incomplete_cost(tmp_path, monkeypatch):
    cases = [
        {"id": "chat", "phase": "pre_contract", "category": "contract"},
        {"id": "import", "phase": "ingest", "category": "contract"},
        {"id": "old", "phase": "ingest", "category": "contract"},
    ]
    ledger = {
        "model_calls": 1, "model_seconds_sum": 2, "prompt_tokens": 40000,
        "completion_tokens": 10000, "cost_cny": 0.01, "model_failures": 0,
        "unknown_usage_calls": 1, "calls": [],
    }
    rows = [
        {"id": "chat", "answer": "ok", "seconds": 3},
        {"id": "import", "contract_id": "ct-1", "upload_seconds": 8, "ledger": ledger},
        {"id": "old", "contract_id": "ct-2", "upload_seconds": 9},
    ]
    case_file, result_file, out = tmp_path / "cases.jsonl", tmp_path / "results.jsonl", tmp_path / "out.json"
    case_file.write_text("\n".join(map(json.dumps, cases)))
    result_file.write_text("\n".join(map(json.dumps, rows)))
    monkeypatch.setattr(sys, "argv", ["summary", "--cases", str(case_file), "--results", str(result_file),
                                      "--out", str(out)])
    summarize_golden.main()
    summary = json.loads(out.read_text())
    assert summary["overall"]["runs"] == 1
    assert summary["overall"]["cost_cny_total"] is None
    imports = summary["contract_import"]["overall"]
    assert imports["runs"] == 2 and imports["latency_seconds"]["n"] == 2
    assert imports["known_cost_cny_all_runs"] == 0.01
    assert imports["missing_ledger_samples"] == 1 and imports["unknown_usage_calls"] == 1
    assert imports["cost_complete"] is False


def test_b_summary_counts_failed_import_spend_and_legacy_ledgers():
    ledger = {
        "model_calls": 1, "model_seconds_sum": 2, "prompt_tokens": 10,
        "completion_tokens": 5, "cost_cny": 0.2, "model_failures": 0, "calls": [],
    }
    stats = summarize_golden.group_stats([
        {"error": "extraction failed", "upload_seconds": 2, "ledger": ledger},
    ], ingest=True)
    assert stats["succeeded"] == 0 and stats["cost_cny_total"] is None
    assert stats["known_cost_cny_all_runs"] == 0.2
    assert stats["legacy_ledger_samples"] == 1 and not stats["cost_complete"]


def test_b_import_uses_http_run_id_to_collect_ledger(db, monkeypatch, tmp_path):
    from backend.documents import load_pages

    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")
    monkeypatch.setattr("backend.llm.client.GLMOCRClient.parse", lambda *_args: {
        "md_results": "Agreement", "usage": {"prompt_tokens": 40000, "completion_tokens": 10000},
    })
    run = db.create_inline_contract_run("dist-1", "u-a", "agreement.pdf")
    load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    db.transition_run("dist-1", run["id"], "succeeded")
    pdf = tmp_path / "agreement.pdf"
    pdf.write_bytes(b"%PDF")
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"id": "con-1", "phase": "ingest", "category": "contract",
                                 "scope": {"partnership_id": "ps-1"}}))
    monkeypatch.setitem(run_golden.AGREEMENTS, "ps-1", str(pdf))
    reads = iter([None, {"contract": {"status": "active"}}])
    monkeypatch.setattr(run_golden, "read_contract", lambda *_args: next(reads))
    args = SimpleNamespace(cases=str(cases), only=None, phase="ingest", inproc=False, base_url="http://test")

    async def collect():
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"id": "ct-1", "discount_rules": 0, "run_id": run["id"]},
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            return await run_golden.main_async(args, client=client, db=db)

    rows = asyncio.run(collect())
    assert rows[0]["run_id"] == run["id"]
    assert rows[0]["ledger"]["cost_cny"] == 0.01
    assert rows[0]["ledger"]["calls"][0]["purpose"] == "document.ocr"
