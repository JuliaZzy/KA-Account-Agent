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
    assert load_pages("agreement.pdf", b"%PDF", db=db, run_id=run["id"]) == [
        (1, "# Agreement\n\nMinimum order USD 500."),
    ]
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
