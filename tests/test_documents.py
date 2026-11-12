"""PDF text comes from GLM-OCR, one string per page. The test never calls the network."""

from __future__ import annotations

import json

import pytest

from backend.documents import load_pages


def _response(payload: dict):
    class _Body:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return _Body()


def test_files_over_10mb_are_refused():
    with pytest.raises(ValueError, match="10MB"):
        load_pages("notes.txt", b"x" * (20 * 1024 * 1024 + 1))


def test_pdf_without_a_key_names_the_variable(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "")
    with pytest.raises(ValueError, match="GLM_API_KEY"):
        load_pages("agreement.pdf", b"%PDF-1.4")


def test_pdf_pages_come_from_GLM_layout(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")
    seen: dict = {}
    payload = {
        "md_results": "whole file, not used when pages are present",
        "layout_details": [
            [
                {"index": 2, "label": "table", "content": "<table><tr><td>USD 500</td></tr></table>"},
                {"index": 1, "label": "text", "content": "Term 2026-01-01"},
                {"index": 3, "label": "image", "content": "https://cdn.example/scan.jpg"},
            ],
            [{"index": 1, "label": "text", "content": "Sweden"}],
        ],
    }

    def fake_urlopen(req, timeout=0):
        body = json.loads(req.data.decode())
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["model"] = body["model"]
        seen["file"] = body["file"]
        seen["capped"] = "end_page_id" in body or "start_page_id" in body
        seen["timeout"] = timeout
        return _response(payload)

    monkeypatch.setattr("backend.llm.client.urllib.request.urlopen", fake_urlopen)
    pages = load_pages("agreement.pdf", b"%PDF-1.4")
    assert pages == [(1, "Term 2026-01-01\n\nUSD 500"), (2, "Sweden")]
    assert seen["url"] == "https://ocr.example/layout_parsing"
    assert seen["auth"] == "Bearer test-key"
    assert seen["model"] == "glm-ocr" and seen["capped"] is False
    assert seen["file"].startswith("data:application/pdf;base64,")
    assert seen["timeout"] == 180


def test_pdf_uses_markdown_when_the_response_has_no_pages(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")

    def fake_urlopen(req, timeout=0):
        return _response({"md_results": "# Agreement\n\nMinimum order USD 500."})

    monkeypatch.setattr("backend.llm.client.urllib.request.urlopen", fake_urlopen)
    assert load_pages("agreement.pdf", b"%PDF") == [(1, "# Agreement\n\nMinimum order USD 500.")]


def test_pdf_ocr_is_a_tool_step_while_the_run_is_running(db, monkeypatch):
    from datetime import timedelta

    from backend.db import utcnow

    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")

    def fake_urlopen(req, timeout=0):
        del req, timeout
        return _response({"md_results": "# Agreement\n\nMinimum order USD 500."})

    monkeypatch.setattr("backend.llm.client.urllib.request.urlopen", fake_urlopen)
    run, _created = db.create_agent_run("dist-1", "u-a", "chat", "ocr-trace", {"message": "read the file"})
    with pytest.raises(ValueError, match="run is not running"):
        load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    assert db.run_trace("dist-1", run["id"])["steps"] == []
    assert db.claim_chat_turn(run["id"], "worker-test", utcnow() + timedelta(minutes=5)) is not None
    pages = load_pages("notes.txt", b"hello\n\nthere", db=db, run_id=run["id"])
    assert pages == [(1, "hello\n\nthere")]
    pages = load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    trace = db.run_trace("dist-1", run["id"])
    assert trace["run"]["status"] == "running"
    assert [(step["step_type"], step["name"], step["status"]) for step in trace["steps"]] == [
        ("tool", "document.ocr", "succeeded"),
    ]
    assert trace["steps"][0]["output_json"] == {"file_name": "agreement.pdf", "pages": len(pages)}
    tool = trace["tool_calls"][0]
    assert tool["tool_name"] == "document.ocr"
    assert tool["status"] == "succeeded"
    assert tool["result_json"]["pages"] == 1
    assert tool["step_id"] == trace["steps"][0]["id"]
    assert trace["model_calls"][0]["usage_source"] == "unknown"
    assert trace["usage_records"][0]["usage_source"] == "unknown"


def test_ocr_actual_usage_enters_the_ledger_even_if_page_parsing_fails(db, monkeypatch):
    from backend.llm.pricing import call_cost_cny

    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")
    payload = {"usage": {"prompt_tokens": 40000, "completion_tokens": 10000}, "request_id": "req-1"}
    monkeypatch.setattr("backend.llm.client.urllib.request.urlopen", lambda *_args, **_kw: _response(payload))
    run = db.create_inline_contract_run("dist-1", "u-a", "agreement.pdf")
    with pytest.raises(ValueError, match="no pages"):
        load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    trace = db.run_trace("dist-1", run["id"])
    call, usage = trace["model_calls"][0], trace["usage_records"][0]
    assert (call["status"], call["usage_source"], call["prompt_tokens"], call["completion_tokens"]) == (
        "succeeded", "actual", 40000, 10000,
    )
    assert trace["tool_calls"][0]["status"] == "failed"
    assert call["response_json"] == {"request_id": "req-1"}
    assert usage["total_tokens"] == 50000
    assert call_cost_cny("glm", "glm-ocr", prompt_tokens=40000, completion_tokens=10000) == 0.01
    assert db.org_daily_usage("dist-1")["estimated_cost_cents"] == 1


def test_ocr_budget_prevents_request_and_does_not_create_a_model_call(db, monkeypatch):
    from backend.llm.calls import ModelBudgetExceeded

    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")
    run = db.create_inline_contract_run("dist-1", "u-a", "agreement.pdf")
    monkeypatch.setattr("backend.llm.client.urllib.request.urlopen", lambda *_args, **_kw: _response({
        "md_results": "Agreement", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }))
    load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    monkeypatch.setenv("KA_ACCOUNT_ORG_DAILY_TOKENS", "15")
    monkeypatch.setattr("backend.llm.client.urllib.request.urlopen", lambda *_args, **_kw: pytest.fail("called"))
    with pytest.raises(ModelBudgetExceeded):
        load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    trace = db.run_trace("dist-1", run["id"])
    assert len(trace["model_calls"]) == 1
    assert [row["status"] for row in trace["tool_calls"]] == ["succeeded", "failed"]


def test_ocr_transport_failure_is_unknown_and_repeated_filenames_are_distinct(db, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")
    run = db.create_inline_contract_run("dist-1", "u-a", "agreement.pdf")

    def fail(*_args, **_kwargs):
        raise OSError("connection lost")

    monkeypatch.setattr("backend.llm.client.urllib.request.urlopen", fail)
    for _ in range(2):
        with pytest.raises(OSError, match="connection lost"):
            load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    trace = db.run_trace("dist-1", run["id"])
    assert len(trace["tool_calls"]) == len(trace["model_calls"]) == 2
    assert all(row["usage_source"] == "unknown" for row in trace["usage_records"])
    assert db.org_daily_usage("dist-1")["unknown_usage_calls"] == 2
    assert db.monitor_facts("dist-1")["daily_unknown_usage_calls"] == 2


def test_partial_ocr_usage_keeps_known_tokens(db, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "test-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://ocr.example/layout_parsing")
    monkeypatch.setattr("backend.llm.client.urllib.request.urlopen", lambda *_args, **_kw: _response({
        "md_results": "Agreement", "usage": {"prompt_tokens": 123},
    }))
    run = db.create_inline_contract_run("dist-1", "u-a", "agreement.pdf")
    load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"])
    usage = db.list_usage_records(run["id"])[0]
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["usage_source"]) == (123, 0, "unknown")


def test_expired_inline_contract_is_failed_without_replay(db, monkeypatch):
    from datetime import timedelta
    from importlib import import_module

    from backend.db import utcnow

    run = db.create_inline_contract_run("dist-1", "u-a", "agreement.pdf")
    monkeypatch.setattr(import_module("backend.db.database.run"), "utcnow", lambda: utcnow() + timedelta(hours=2))
    assert db.sweep_expired_leases() == 1
    trace = db.run_trace("dist-1", run["id"])
    assert trace["run"]["status"] == "failed"
    assert trace["outbox"] == []
