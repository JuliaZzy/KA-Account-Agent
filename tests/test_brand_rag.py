"""Brand questions go to retrieval as typed, then the existing text model answers from the hits.

No Milvus and no embedding endpoint: retrieve is stubbed, chunking is pure.
"""

from __future__ import annotations

import json
import math

import pytest

from backend import service
from backend.brand_knowledge import RagUnavailable, chunks_from_pages, evidence_prompt
from backend.brand_knowledge.chunk import CHUNK_CHARS
from backend.brand_knowledge.rag import (
    bm25_search,
    evaluate_retrieval,
    reciprocal_rank_fusion,
    rerank,
    source_label,
    tokenize,
)
from backend.config import rerank_settings
from backend.llm import FakeLLM
from backend.llm.types import AssistantTurn, ToolCall
from backend.orchestrator.prompt import FOLLOWUP
from backend.schemas import BrandDocument
from backend.services.chat_run import work_chat


def test_headings_end_a_chunk_and_the_path_prefixes_the_next():
    pages = [(
        1,
        "# 品牌故事\n\n创立于上海。\n\n## 冷萃\n\n用十二小时制作。\n\n"
        "1. 产品\n\n概述如下。\n\n1.1 冷萃\n\n十二小时。\n\n1.2 美式\n\n短时萃取。\n\n"
        "2. 门店\n\n上海。\n\n规格：\n\n瓶装。",
    )]
    chunks = chunks_from_pages("doc-1", pages)
    assert [c["text"] for c in chunks] == [
        "品牌故事\n\n创立于上海。",
        "品牌故事 > 冷萃\n\n用十二小时制作。",
        "1. 产品\n\n概述如下。",
        "1. 产品 > 1.1 冷萃\n\n十二小时。",
        "1. 产品 > 1.2 美式\n\n短时萃取。",
        "2. 门店\n\n上海。",
        "规格\n\n瓶装。",
    ]
    assert chunks[0]["cid"] == "doc-1#p1-0"


def test_a_paragraph_stays_whole_and_a_wrap_joins_across_pages():
    sentence = "甲" * 40 + "。"
    count = (CHUNK_CHARS // len(sentence)) + 1
    packed = chunks_from_pages("doc-1", [(1, "\n\n".join([sentence] * count))])
    assert len(packed) >= 2
    assert all(len(c["text"]) <= CHUNK_CHARS for c in packed)
    assert "\n".join(c["text"] for c in packed) == "\n".join([sentence] * count)
    assert "。甲" not in packed[0]["text"]

    joined = chunks_from_pages("doc-1", [(2, "冷萃用十二小时制作，"), (3, "水温保持在四度。")])
    assert joined == [{
        "doc": "doc-1", "page": 2, "cid": "doc-1#p2-0",
        "text": "冷萃用十二小时制作，水温保持在四度。",
    }]
    english = chunks_from_pages("doc-1", [(1, "cold\n\nbrew.")])
    assert english[0]["text"] == "cold brew."
    braced = chunks_from_pages("doc-1", [(1, "配方包含 {水、咖啡豆}，冲泡即可。")])
    assert braced[0]["text"] == "配方包含 {水、咖啡豆}，冲泡即可。"


def test_a_long_paragraph_splits_on_sentences_and_keeps_the_section():
    sentence = "甲" * 180 + "。"
    chunks = chunks_from_pages("doc-1", [(5, "# 冷萃\n\n" + sentence * 4)])
    assert [c["text"].count("。") for c in chunks] == [2, 2]
    assert all(c["page"] == 5 and c["text"].startswith("冷萃\n\n") for c in chunks)
    assert "".join(c["text"].removeprefix("冷萃\n\n") for c in chunks) == sentence * 4

    blob = "乙" * (CHUNK_CHARS + 10)
    hard = chunks_from_pages("doc-1", [(3, "# 冷萃\n\n" + blob)])
    assert all(len(c["text"]) <= CHUNK_CHARS for c in hard)
    assert "".join(c["text"].removeprefix("冷萃\n\n") for c in hard) == blob


def test_a_table_is_its_own_chunk_and_a_long_one_repeats_the_header():
    header = "产品 | 规格 | 价格"
    row = "冷萃 | 500ml | 36元"
    table = "\n".join([header, *([row] * 40)])
    page = "# 价目\n\n以下为建议零售价。\n\n" + table
    chunks = chunks_from_pages("doc-1", [(4, page)])
    assert chunks[0]["text"] == "价目\n\n以下为建议零售价。"
    priced = chunks[1:]
    assert priced and all(c["page"] == 4 and c["text"].startswith("价目\n\n" + header) for c in priced)
    assert sum(c["text"].count("36元") for c in priced) == 40


def test_bm25_keeps_a_measure_whole_and_ranks_that_chunk_first():
    assert tokenize("冷萃 500ml") == ["冷", "萃", "冷萃", "500ml"]
    assert tokenize("冷萃咖啡") == ["冷", "萃", "咖", "啡", "冷萃", "萃咖", "咖啡"]
    corpus = [
        {"cid": "a", "doc": "d", "page": 1, "text": "冷萃咖啡，焦糖尾韵。"},
        {"cid": "b", "doc": "d", "page": 2, "text": "冷萃 500ml，建议零售价 36 元。"},
    ]
    assert bm25_search("500ml", corpus)[0]["cid"] == "b"


def test_fusion_keeps_a_hit_found_by_only_one_list():
    dense = [{"cid": "a", "doc": "d", "page": 1, "text": "a", "score": 0.9}]
    sparse = [
        {"cid": "b", "doc": "d", "page": 2, "text": "b", "score": 3},
        {"cid": "a", "doc": "d", "page": 1, "text": "a", "score": 1},
    ]
    fused = reciprocal_rank_fusion([dense, sparse])
    assert [hit["cid"] for hit in fused] == ["a", "b"]


def test_the_reranker_defaults_to_the_bge_cross_encoder(monkeypatch):
    monkeypatch.delenv("RERANK_MODEL", raising=False)
    assert rerank_settings().model == "BAAI/bge-reranker-v2-m3"


def test_retrieval_metrics_score_the_labeled_run():
    # q1 relevant at ranks 1 and 3; q2 relevant at rank 2. Precision divides by k, not by list length.
    scores = evaluate_retrieval(
        {"q1": {"a": 1, "b": 1}, "q2": ["c"]},
        {
            "q1": [
                {"cid": "a", "score": 0.9},
                {"cid": "d", "score": 0.5},
                {"cid": "b", "score": 0.1},
            ],
            "q2": [
                {"cid": "a", "score": 0.4},
                {"cid": "c", "score": 0.3},
            ],
        },
        k=5,
    )
    ndcg_q1 = 1.5 / (1 + 1 / math.log2(3))
    ndcg_q2 = 1 / math.log2(3)
    assert scores == pytest.approx({
        "mrr": 0.75,
        "ndcg@5": (ndcg_q1 + ndcg_q2) / 2,
        "precision@5": 0.3,
        "recall@5": 1.0,
        "map": (5 / 6 + 0.5) / 2,
        "hit_rate@5": 1.0,
    })


def test_ndcg_uses_the_grade_and_a_miss_scores_zero():
    # Both chunks are relevant, so MAP stays 1; the better grade is second, so nDCG is not 1.
    graded = evaluate_retrieval(
        {"q": {"z": 2, "y": 1}},
        {"q": [{"cid": "y", "score": 0.2}, {"cid": "z", "score": 0.1}]},
        k=5,
    )
    perfect = (3 + 1 / math.log2(3))
    actual = (1 + 3 / math.log2(3))
    assert graded["map"] == pytest.approx(1.0)
    assert graded["ndcg@5"] == pytest.approx(actual / perfect)
    assert graded["ndcg@5"] < 1

    missed = evaluate_retrieval({"q": {"a": 1}}, {}, k=5)
    assert missed == pytest.approx({
        "mrr": 0.0, "ndcg@5": 0.0, "precision@5": 0.0, "recall@5": 0.0, "map": 0.0, "hit_rate@5": 0.0,
    })


def test_a_hit_without_a_score_keeps_its_place():
    scores = evaluate_retrieval({"q": ["a"]}, {"q": [{"cid": "b"}, {"cid": "a"}]}, k=5)
    assert scores["mrr"] == pytest.approx(0.5)


def test_rerank_is_skipped_until_a_model_is_configured(monkeypatch):
    monkeypatch.setenv("RERANK_BASE_URL", "")
    hits = [{"cid": str(i), "doc": "d", "page": 1, "text": "t", "score": 1} for i in range(8)]
    assert [hit["cid"] for hit in rerank("500ml", hits)] == ["0", "1", "2", "3", "4"]


def test_rerank_applies_the_cross_encoder_order(monkeypatch):
    monkeypatch.setenv("RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setenv("RERANK_API_KEY", "test-key")
    monkeypatch.setenv("RERANK_MODEL", "bge-reranker-v2-m3")
    seen = {}

    def fake_urlopen(req, timeout=0):
        body = json.loads(req.data.decode())
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["model"] = body["model"]
        seen["docs"] = body["documents"]

        class _Body:
            def read(self):
                return json.dumps({"results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return _Body()

    monkeypatch.setattr("backend.brand_knowledge.rag.urllib.request.urlopen", fake_urlopen)
    hits = [
        {"cid": "a", "doc": "d", "page": 1, "text": "alpha", "score": 0.1},
        {"cid": "b", "doc": "d", "page": 2, "text": "beta", "score": 0.2},
    ]
    out = rerank("500ml", hits)
    assert [hit["cid"] for hit in out] == ["b", "a"]
    assert out[0]["score"] == 0.9
    assert seen == {
        "url": "https://rerank.example/v1/rerank",
        "auth": "Bearer test-key",
        "model": "bge-reranker-v2-m3",
        "docs": ["alpha", "beta"],
    }


def test_evidence_prompt_numbers_passages_and_names_the_file():
    assert source_label("品牌手册.pdf", 3) == "品牌手册.pdf，第3页"
    filled = evidence_prompt("how long?", [{
        "cid": "d#p1-0", "doc": "d", "page": 1, "text": "8 hours",
        "source": "品牌手册.pdf，第1页",
    }])
    assert filled.startswith("段落：\n[1] 品牌手册.pdf，第1页\n8 hours")
    assert "how long?" in filled
    plain = evidence_prompt("how long?", [{"cid": "d#p1-0", "doc": "d", "page": 1, "text": "8 hours"}])
    assert "[1] d，第1页" in plain
    assert "（没有检索到段落）" in evidence_prompt("how long?", [])


def test_brand_question_is_retrieved_then_answered_by_the_text_model(db, monkeypatch):
    seen = {}

    def fake_retrieve(question, brand_id):
        seen["q"] = question
        seen["brand"] = brand_id
        return [{"cid": "doc#p1-0", "doc": "doc", "page": 1, "text": "Runs 8 hours at 42 C.", "score": 0.9}]

    monkeypatch.setattr("backend.services.session_tools.retrieve", fake_retrieve)
    db.save_document(BrandDocument(id="doc", brand_id="brand-1", file_name="品牌手册.pdf"), [(1, "Runs 8 hours at 42 C.")])
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = service.new_session(db, "ps-1", "u-a")["id"]
    llm = FakeLLM(
        json_replies=[{"tags": ["brand"], "title": "runtime"}],
        tool_replies=[AssistantTurn(tool_calls=[ToolCall(id="c1", name="brand.search", arguments={})])],
        text_reply="It runs 8 hours [doc#p1-0].",
    )
    events = list(service.handle_message(db, llm, sid, "How long does it run at 42 C?"))
    assert work_chat(db, llm, events[0]["run_id"]) is True
    assert seen == {"q": "How long does it run at 42 C?", "brand": "brand-1"}
    stored = db.session(sid)["history"][-1]
    assert stored["content"] == "It runs 8 hours [doc#p1-0]."
    assert stored["sources"] == [{
        "n": 1, "cid": "doc#p1-0", "doc": "doc", "page": 1,
        "file_name": "品牌手册.pdf", "source": "品牌手册.pdf，第1页",
    }]


def test_a_follow_up_is_rewritten_for_retrieval_and_stored_as_typed(db, monkeypatch):
    seen = []

    def fake_retrieve(question, brand_id):
        seen.append((question, brand_id))
        return [{"cid": "doc#p1-0", "doc": "doc", "page": 1, "text": "冷萃 500ml，保质期十二个月。", "score": 0.9}]

    monkeypatch.setattr("backend.services.session_tools.retrieve", fake_retrieve)
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = service.new_session(db, "ps-1", "u-a")["id"]
    llm = FakeLLM(
        json_replies=[
            {"tags": ["brand"], "title": "冷萃"},
            {"query": "冷萃 500ml 的保质期"},
            {"query": ""},
        ],
        tool_replies=[
            AssistantTurn(tool_calls=[ToolCall(id="c1", name="brand.search", arguments={})]),
            AssistantTurn(content="十二个月 [1]。"),
            AssistantTurn(tool_calls=[ToolCall(id="c2", name="brand.search", arguments={})]),
            AssistantTurn(content="十二个月 [1]。"),
            AssistantTurn(tool_calls=[ToolCall(id="c3", name="brand.search", arguments={})]),
            AssistantTurn(content="十二个月 [1]。"),
        ],
        text_reply="十二个月 [1]。",
    )
    for text in ("冷萃 500ml 保质期多久", "那瓶装的呢", "你好"):
        events = list(service.handle_message(db, llm, sid, text))
        work_chat(db, llm, events[0]["run_id"])
    assert seen == [
        ("冷萃 500ml 保质期多久", "brand-1"),
        ("冷萃 500ml 的保质期", "brand-1"),
        ("你好", "brand-1"),
    ]
    user_lines = [m["content"] for m in db.session(sid)["history"] if m["role"] == "user"]
    assert user_lines[-2:] == ["那瓶装的呢", "你好"]
    complete = [c for c in llm.calls if c["kind"] == "complete"]
    assert any("那瓶装的呢" in str(c["messages"]) for c in complete)
    rewrites = [c for c in llm.calls if c["kind"] == "json" and (
        c["system"] is FOLLOWUP or (isinstance(c["system"], str) and c["system"].startswith(FOLLOWUP[:8]))
    )]
    assert len(rewrites) == 2
    assert "冷萃 500ml 保质期多久" in rewrites[0]["messages"][-1]["content"]
    assert "那瓶装的呢" in rewrites[0]["messages"][-1]["content"]


def test_unconfigured_retrieval_is_told_to_the_user(db, monkeypatch):
    def boom(question, brand_id):
        raise RagUnavailable("EMBEDDING_BASE_URL is not set.")

    monkeypatch.setattr("backend.services.session_tools.retrieve", boom)
    db.upsert_user("u-a", "dist-1", "Ada")
    sid = service.new_session(db, "ps-1", "u-a")["id"]
    llm = FakeLLM(
        json_replies=[{"tags": ["brand"], "title": "specs"}],
        tool_replies=[AssistantTurn(tool_calls=[ToolCall(id="c1", name="brand.search", arguments={})])],
        text_reply="should not be used",
    )
    events = list(service.handle_message(db, llm, sid, "What is the shelf life?"))
    assert work_chat(db, llm, events[0]["run_id"]) is True
    trace = db.run_trace("dist-1", events[0]["run_id"])
    call = next(item for item in trace["tool_calls"] if item["tool_name"] == "brand.search")
    assert "EMBEDDING_BASE_URL" in str(call.get("result_json") or call.get("error_json"))
