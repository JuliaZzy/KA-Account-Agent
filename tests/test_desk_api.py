"""The desk's HTTP surface: the pages the web client renders, against the same database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.contract import ingest_extraction
from backend.llm import FakeLLM
from backend.schemas import Partnership, Product


def client(db, llm=None) -> TestClient:
    return TestClient(create_app(db=db, llm=llm or FakeLLM()))


def test_meta_lists_the_official_models(db):
    from backend import service

    body = client(db).get("/api/meta").json()
    ids = [m["id"] for m in body["models"]]
    assert "qwen3.7-flash" in ids
    assert "deepseek-flash" in ids
    assert body["default_model"] == "qwen3.7-flash"
    assert "停产" in body["product_statuses"]
    assert body["memory_kinds"]["preference"] == "Preference"
    assert body["max_images"] == service.MAX_IMAGES


def test_a_turn_rejects_more_images_than_meta(db):
    from backend import service

    sid = service.new_session(db, "ps-1")["id"]
    cap = service.MAX_IMAGES
    images = [f"data:image/png;base64,xx{i}" for i in range(cap + 1)]
    resp = client(db).post(f"/api/sessions/{sid}/message", json={"message": "hi", "images": images})
    assert resp.status_code == 400
    assert str(cap) in resp.json()["detail"]


def test_catalog_filters_and_needs_no_partnership(db, make_db):
    c = client(db)
    listed = c.get("/api/catalog").json()
    assert {x["sku"] for x in listed["items"]} == {"FO-100", "SR-200", "SR-201", "NP-300", "GS-010", "GU-020", "OLD-900"}
    assert {x["brand_name"] for x in listed["items"]} == {"Brand"}

    native = c.get("/api/catalog", params={"q": "鱼油"}).json()
    assert [x["sku"] for x in native["items"]] == ["FO-100"]

    retired = c.get("/api/catalog", params={"status": "停产"}).json()
    assert [x["sku"] for x in retired["items"]] == ["OLD-900"]

    bare = make_db("bare")
    bare.upsert_brand("b-solo", "Solo Foods")
    bare.upsert_product("b-solo", Product(id="p-1", sku="SF-1", product_name_en="Oat Drink", product_name_native="Haferdrink"))
    solo = client(bare)
    assert solo.get("/api/partnerships").json() == []
    assert solo.get("/api/catalog").json()["items"][0]["product_name_native"] == "Haferdrink"


def test_sessions_list_is_newest_first(db):
    c = client(db)
    first = c.post("/api/sessions", json={"partnership_id": "ps-1"}).json()["id"]
    second = c.post("/api/sessions", json={"partnership_id": "ps-1"}).json()["id"]
    rows = c.get("/api/sessions", params={"partnership_id": "ps-1"}).json()
    assert [r["id"] for r in rows[:2]] == [second, first]
    assert "history" not in rows[0]


def test_orders_include_the_submitted_day(db):
    from datetime import timedelta

    from backend.po.check import run_code_check
    from backend.po.slots import Discount, LineItemSlot, PoSlots
    from backend.po.submit import create_and_submit_po
    from backend.po.turn import load_bundle

    bundle = load_bundle(db, "ps-1")
    slots = PoSlots(line_items=[LineItemSlot(reference="FO-100", cases=20)], discount=Discount(kind="none"))
    check = run_code_check(slots=slots, catalog=bundle.catalog, rules=bundle.rules, partnership_id="ps-1", ship_to_country="US")
    result = create_and_submit_po(db, partnership=bundle.partnership, settings=bundle.settings, slots=slots, check=check)
    submitted_at = db.po(result.po_id).submitted_at
    assert submitted_at.tzinfo is not None and submitted_at.utcoffset() == timedelta(0)

    body = client(db).get("/api/orders", params={"partnership_id": "ps-1"}).json()
    row = next(o for o in body if o["po_number"] == result.po_number)
    assert row["submitted_at"].startswith(submitted_at.date().isoformat())
    assert row["items"][0]["sku"] == "FO-100"


def test_contract_detail_shows_the_next_discount_and_derived_rules(db, partnership: Partnership):
    rec = ingest_extraction(
        db,
        {
            "minimum_order_quantities": [{"quantity": 500, "unit": "USD", "applies_per": "per_order"}],
            "commercial_discount_rules": [{
                "title": "Container sequence discount",
                "tiers": [{"from_container": 1, "to_container": 2, "discount_percent": 3}],
            }],
        },
        brand_id=partnership.brand_id, partnership_id=partnership.id, title="Distribution 2026",
    )
    body = client(db).get(f"/api/contracts/{rec.contract.id}").json()
    assert body["contract"]["title"] == "Distribution 2026"
    assert body["next_discount"]["percent"] == 3
    assert len(body["terms"]["discount_rules"]) == 1
    assert any(r["rule_type"] == "min_order_value" for r in body["rules"])


def test_chats_and_memories_belong_to_one_person(db):
    db.upsert_user("u-a", "dist-1", "Ada")
    db.upsert_user("u-b", "dist-1", "Bea")
    c = client(db)
    assert {row["name"] for row in c.get("/api/users", params={"distributor_id": "dist-1"}).json()} == {"Ada", "Bea"}
    ada = c.post("/api/sessions", json={"partnership_id": "ps-1", "user_id": "u-a"}).json()["id"]
    c.post("/api/sessions", json={"partnership_id": "ps-1", "user_id": "u-b"})
    assert [row["id"] for row in c.get("/api/sessions", params={"partnership_id": "ps-1", "user_id": "u-a"}).json()] == [ada]
    assert c.post("/api/sessions", json={"partnership_id": "ps-1", "user_id": "missing"}).status_code == 400
    c.post("/api/partnerships/ps-1/memories", json={"content": "Ada likes sea freight", "kind": "fact", "user_id": "u-a"})
    c.post("/api/partnerships/ps-1/memories", json={"content": "Bea likes air freight", "kind": "fact", "user_id": "u-b"})
    seen = c.get("/api/partnerships/ps-1/memories", params={"user_id": "u-a"}).json()
    assert [row["content"] for row in seen["active"]] == ["Ada likes sea freight"]
    assert "Bea" not in seen["block"]
    # The order list is the partnership's, so both people still see it.
    assert c.get("/api/orders", params={"partnership_id": "ps-1"}).status_code == 200


def test_memory_round_trip(db):
    c = client(db)
    assert c.get("/api/partnerships/ps-1/memories").json()["active"] == []
    saved = c.post("/api/partnerships/ps-1/memories", json={"content": "Prefers air freight for launches", "kind": "preference"}).json()
    assert saved["content"] == "Prefers air freight for launches" and saved["source"] == "user"
    active = c.get("/api/partnerships/ps-1/memories").json()
    assert "Prefers air freight for launches" in active["block"]
    assert c.patch(f"/api/memories/{saved['id']}", json={"active": False}).status_code == 200
    after = c.get("/api/partnerships/ps-1/memories").json()
    assert after["active"] == [] and after["inactive"][0]["is_active"] is False
    assert c.post("/api/partnerships/ps-1/memories", json={"content": "  ", "kind": "fact"}).status_code == 400


def test_report_confirm_and_delete(db, partnership: Partnership):
    c = client(db)
    csv = b"SKU,Units sold,Sales (USD)\nFO-100,120,1200\nSR-200,30,1500\n"
    preview = c.post(
        "/api/partnerships/ps-1/reports/preview",
        files={"file": ("aug.csv", csv, "text/csv")},
        data={"period": "2025-08"},
    ).json()
    assert preview["fact_count"] == 4

    saved = c.post(
        "/api/partnerships/ps-1/reports",
        files={"file": ("aug.csv", csv, "text/csv")},
        data={"period": "2025-08", "title": "August"},
    ).json()
    assert saved["title"] == "August" and saved["status"] == "draft"
    out = c.post(f"/api/reports/{saved['id']}/confirm").json()
    assert out["confirmed"] == 4
    facts = c.get("/api/partnerships/ps-1/facts").json()
    assert len(facts) == 4
    assert c.delete(f"/api/reports/{saved['id']}").status_code == 204
    assert db.report(saved["id"]) is None


def test_confirming_chat_drafts_files_them(db):
    from backend import service

    sid = service.new_session(db, "ps-1")["id"]
    extraction = {
        "term_start_date": "2026-01-01",
        "term_end_date": "2027-12-31",
        "territory_text": "Sweden",
        "allowed_countries": ["SE"],
        "excluded_countries": [],
        "minimum_order_quantities": [{"quantity": 500, "unit": "USD", "applies_per": "per_order"}],
        "commercial_discount_rules": [{
            "title": "Container sequence", "applies_per": "each_contract_year", "basis": "shipping_container_sequence",
            "tiers": [{"from_container": 1, "to_container": 2, "discount_percent": 3}],
        }],
        "product_price_list": [],
    }
    filing = {
        "pending_id": "cp_test", "title": "Draft", "file_name": "agreement.txt", "confirmed": False,
        "term_start_date": "2026-01-01", "discount_rules": extraction["commercial_discount_rules"],
        "moqs": extraction["minimum_order_quantities"],
        "territories": [{"country_code": "SE", "kind": "allowed"}], "price_list": [],
    }
    db.update_session(sid, history=[
        {
            "role": "assistant", "content": "ready", "contract": filing,
            "contract_pending": {"id": "cp_test", "extraction": extraction, "chunks": [], "file_name": "agreement.txt"},
        },
        {
            "role": "assistant", "content": "# September\n\nSold well.", "report_draft": True, "report_pending_id": "rp_test",
            "charts": [{"id": "chart_001", "title": "Units", "subtitle": "", "echarts_option": {"series": [{"data": [1]}]}}],
        },
    ])
    c = client(db)
    loaded = c.get(f"/api/sessions/{sid}").json()
    assert "contract_pending" not in loaded["history"][0]
    assert loaded["history"][0]["contract"]["pending_id"] == "cp_test"

    edited = {**filing, "title": "Nordic edited", "discount_rules": [{
        **extraction["commercial_discount_rules"][0],
        "tiers": [{"from_container": 1, "to_container": 2, "discount_percent": 8}],
    }]}
    filed = c.post(f"/api/sessions/{sid}/confirm-contract", json={"pending_id": "cp_test", "filing": edited}).json()
    assert filed["contract"]["confirmed"] is True and filed["contract"]["title"] == "Nordic edited"
    listed = c.get("/api/partnerships/ps-1/contracts").json()
    assert listed[0]["id"] == filed["contract_id"] and listed[0]["title"] == "Nordic edited"
    detail = c.get(f"/api/contracts/{filed['contract_id']}").json()
    assert detail["terms"]["discount_rules"][0]["tiers"][0]["discount_percent"] == 8

    assert c.post(f"/api/sessions/{sid}/confirm-report", json={"pending_id": "rp_test", "text": "  "}).status_code == 400
    saved = c.post(f"/api/sessions/{sid}/confirm-report", json={
        "pending_id": "rp_test", "title": "September review", "text": "# September\n\nEdited.",
        "charts": [{"id": "chart_001", "title": "Units", "subtitle": "month", "echarts_option": {"series": [{"data": [3]}]}}],
    }).json()
    writes = c.get("/api/partnerships/ps-1/narratives").json()
    assert writes[0]["id"] == saved["id"] and writes[0]["title"] == "September review"
    assert "Edited." in writes[0]["body"] and writes[0]["charts"][0]["title"] == "Units"
    again = c.post(f"/api/sessions/{sid}/confirm-report", json={
        "pending_id": "rp_test", "title": "September review", "text": "Second draft.", "charts": [],
    }).json()
    assert again["id"] == saved["id"]
    assert len(c.get("/api/partnerships/ps-1/narratives").json()) == 1
