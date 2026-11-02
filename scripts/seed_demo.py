"""One distributor, fifteen brands, orders and sell-out from 2024-01 through 2026-09.

    ./dev py scripts/seed_demo.py
    ./dev py scripts/seed_demo.py --accounts

`--accounts` only adds 海纳分销 (Activia, Red Bull, and three brands 联采贸易 does not
sell) and three people on each distributor. It does not rebuild orders.

October 2026 is left empty on purpose: the first purchase order of this test is the one
placed in the app. Sell-out stops in the same month, so a report for "this month" has
nothing yet and a report for the previous month does.

The catalog in this database was empty, so when fewer than fifteen brands already have
four priced products the script loads a small Open Food Facts slice — the same source as
`./dev catalog`, not a made-up brand list. Product names and barcodes are theirs (ODbL);
case price, pack and MOQ are derived by `build_catalog.py`. A later full catalog load
keeps any of these products an order line points at.

Running it again replaces this distributor's demo orders and sell-out reports. Chats,
the distributor, and the partnerships stay.
"""

from __future__ import annotations

import calendar
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_catalog  # noqa: E402

from backend.db import Database  # noqa: E402
from backend.schemas import Partnership, Product, PurchaseOrder, ReportFact, SalesReport  # noqa: E402

DIST_ID = "dist-liancai"
DIST_NAME = "联采贸易"
DIST2_ID = "dist-haina"
DIST2_NAME = "海纳分销"
# Activia and Red Bull are shared with 联采贸易. The other three must not be.
SHARED_TAGS = ("activia", "red-bull")
EXTRA_TAGS = ("lavazza", "lipton", "quaker")
LIANCAI_USERS = (("user-liancai-chen", "陈思远"), ("user-liancai-lin", "林晓雯"), ("user-liancai-zhou", "周启明"))
HAINA_USERS = (("user-haina-zhao", "赵宁"), ("user-haina-wu", "吴倩"), ("user-haina-zheng", "郑浩"))
CONTRACT_PATH = ROOT / "fixtures" / "demo" / "联采贸易-经销协议.md"
CACHE_PATH = ROOT / "fixtures" / "demo" / "off-slice.json"
UA = "KAAccountAgentDemo/1.0 (local sales seed; not a crawl)"
# More than fifteen: the API sometimes 503s, and some tags have no usable pack size.
BRAND_TAGS = [
    "barilla", "kellogg-s", "nestle", "oreo", "haribo", "ferrero", "lavazza", "lindt",
    "heinz", "danone", "lipton", "pringles", "milka", "evian", "bonduelle", "maggi",
    "activia", "red-bull", "quaker", "ritter-sport", "nescafe", "twinings", "coca-cola",
]
CHANNELS = (("Tmall", 0.46), ("JD", 0.32), ("Offline", 0.22))
SEASON = {1: 0.85, 2: 0.70, 3: 0.90, 4: 0.95, 5: 1.00, 6: 1.35, 7: 0.90, 8: 0.95, 9: 1.05, 10: 1.10, 11: 1.55, 12: 1.40}
FIRST = (2024, 1)
LAST = (2026, 9)  # through September; October is the month under test


def months() -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = FIRST
    while (y, m) <= LAST:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _get(url: str) -> dict:
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            wait = 2 * (attempt + 1)
            print(f"  retry {url.split('brands_tags=')[-1].split('&')[0]} in {wait}s: {err}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Open Food Facts did not answer for {url}")


def _off_row(product: dict, tag: str) -> dict:
    brands = [part.strip() for part in str(product.get("brands") or "").split(",") if part.strip()]
    return {
        "code": str(product.get("code") or ""),
        "name_en": str(product.get("product_name") or "").strip(),
        "name_native": str(product.get("product_name") or "").strip(),
        "generic_name_en": str(product.get("generic_name") or "").strip(),
        "brand": brands[0] if brands else tag,
        "brand_tag": tag,
        "quantity": str(product.get("quantity") or ""),
        "product_quantity": product.get("product_quantity"),
        "product_quantity_unit": str(product.get("product_quantity_unit") or ""),
        "categories_tags": list(product.get("categories_tags") or []),
        "labels_tags": list(product.get("labels_tags") or []),
        "obsolete": bool(product.get("obsolete")),
    }


def fetch_slice() -> list[dict]:
    """Up to eight regular products for each brand tag, until fifteen brands qualify."""
    if CACHE_PATH.is_file():
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if len(cached) >= 15:
            print(f"using {CACHE_PATH.relative_to(ROOT)} ({len(cached)} brands)")
            return cached
    found: list[dict] = []
    dropped: Counter = Counter()
    for tag in BRAND_TAGS:
        if len(found) >= 15:
            break
        url = (
            "https://world.openfoodfacts.org/api/v2/search?brands_tags="
            f"{tag}&page_size=24&fields=code,product_name,generic_name,brands,quantity,"
            "product_quantity,product_quantity_unit,categories_tags,labels_tags,obsolete"
        )
        try:
            payload = _get(url)
        except RuntimeError as err:
            print(f"  skip {tag}: {err}")
            continue
        products: list[dict] = []
        spellings: Counter = Counter()
        for raw in payload.get("products") or []:
            row = _off_row(raw, tag)
            derived = build_catalog.product(row, dropped)
            if not derived or derived["product_status"] != "正品":
                continue
            products.append(derived)
            spellings[row["brand"]] += 1
            if len(products) >= 8:
                break
        if len(products) < 4:
            print(f"  skip {tag}: only {len(products)} usable products")
            continue
        name = spellings.most_common(1)[0][0] if spellings else tag
        found.append({"id": build_catalog.brand_id(tag), "name": name[:200], "products": products})
        print(f"  {name}: {len(products)} products")
    if len(found) < 15:
        raise SystemExit(f"needed 15 brands from Open Food Facts, got {len(found)}")
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    return found


def _qualified(db: Database) -> list[tuple]:
    with db.engine.connect() as conn:
        return list(conn.execute(text(
            """
            SELECT b.id, b.name, b.preferred_currency
            FROM brands b
            JOIN products p ON p.brand_id = b.id
            WHERE p.case_price IS NOT NULL AND p.case_price > 0
              AND p.product_status = '正品'
            GROUP BY b.id, b.name, b.preferred_currency
            HAVING count(p.id) >= 4
            ORDER BY count(p.id) DESC, b.name
            LIMIT 15
            """
        )))


def _products(db: Database, brand_id: str) -> list[Product]:
    with db.engine.connect() as conn:
        rows = conn.execute(text(
            """
            SELECT id, sku, product_name_en, product_name_native, case_pack, case_price, moq_units
            FROM products
            WHERE brand_id = :b AND case_price > 0 AND product_status = '正品'
            ORDER BY sku
            LIMIT 6
            """
        ), {"b": brand_id})
        return [Product(
            id=r.id, sku=r.sku, product_name_en=r.product_name_en, product_name_native=r.product_name_native,
            case_pack=r.case_pack, case_price=r.case_price, moq_units=r.moq_units,
        ) for r in rows]


def ensure_catalog(db: Database) -> list[tuple]:
    current = _existing_partnerships(db)
    if len(current) >= 15:
        # Keep the brands this distributor already sells. A later brand (海纳分销's
        # extra three) must not displace one of them just because it has more products.
        print(f"distributor already has {len(current)} partnerships; keeping those brands")
        with db.engine.connect() as conn:
            return list(conn.execute(text(
                """
                SELECT b.id, b.name, b.preferred_currency
                FROM partnerships p
                JOIN brands b ON b.id = p.brand_id
                WHERE p.distributor_id = :d
                ORDER BY b.name
                LIMIT 15
                """
            ), {"d": DIST_ID}))
    have = _qualified(db)
    if len(have) >= 15:
        print(f"catalog already has {len(have)} brands with priced products; using those")
        return have
    print("catalog has fewer than 15 priced brands; loading an Open Food Facts slice")
    for brand in fetch_slice():
        db.upsert_brand(brand["id"], brand["name"], "USD")
        db.upsert_products(brand["id"], [Product(**{k: p[k] for k in Product.model_fields if k in p}) for p in brand["products"]])
    have = _qualified(db)
    if len(have) < 15:
        raise SystemExit(f"after loading the slice, only {len(have)} brands qualify")
    return have[:15]


def _existing_partnerships(db: Database) -> list[tuple]:
    with db.engine.connect() as conn:
        return list(conn.execute(text(
            """
            SELECT p.id, p.brand_id, b.name, p.currency
            FROM partnerships p
            JOIN brands b ON b.id = p.brand_id
            WHERE p.distributor_id = :d
            ORDER BY b.name
            """
        ), {"d": DIST_ID}))


def ensure_partnerships(db: Database, brands: list[tuple]) -> list[dict]:
    db.upsert_distributor(DIST_ID, DIST_NAME, "CN")
    current = _existing_partnerships(db)
    by_brand = {row.brand_id: row for row in current}
    chosen = []
    for index, brand in enumerate(brands, start=1):
        row = by_brand.get(brand.id)
        pid = row.id if row else f"ps-liancai-{index:02d}"
        currency = (row.currency if row else None) or brand.preferred_currency or "USD"
        db.upsert_partnership(Partnership(
            id=pid, brand_id=brand.id, distributor_id=DIST_ID,
            brand_name=brand.name, distributor_name=DIST_NAME,
            ship_to_country="CN", currency=currency,
        ))
        chosen.append({
            "partnership_id": pid, "brand_id": brand.id, "brand_name": brand.name,
            "currency": currency, "index": index, "products": _products(db, brand.id),
        })
        if len(chosen) == 15:
            break
    return chosen


def _clear(db: Database) -> None:
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM purchase_orders WHERE id LIKE 'po-demo-%'"))
        conn.execute(text("DELETE FROM sales_reports WHERE id LIKE 'rpt-demo-%'"))


def _scale(year: int, month: int) -> float:
    return SEASON[month] * (1.08 ** (year - 2024))


def _cases(product: Product, year: int, month: int, slot: int) -> int:
    pack = product.case_pack or 12
    moq_cases = -(-(product.moq_units or pack) // pack)  # ceil
    base = round((18 + 6 * slot) * _scale(year, month))
    return max(moq_cases, base)


def _status(year: int, month: int) -> str:
    if (year, month) <= (2026, 6):
        return "completed"
    if (year, month) <= (2026, 8):
        return "awaiting_payment"
    return "submitted"


def _money(status: str, terms: str, total: float) -> tuple[float, float]:
    half = round(total / 2, 2)
    if status == "completed":
        if terms == "100_prepaid":
            return total, 0.0
        if terms == "50_50":
            return half, round(total - half, 2)
        return 0.0, total
    if status == "awaiting_payment" and terms == "50_50":
        return half, 0.0
    return 0.0, 0.0


def _seed_orders(db: Database, link: dict) -> int:
    terms_cycle = ("net_30", "50_50", "100_prepaid")
    n = 0
    for ordinal, (year, month) in enumerate(months()):
        products = link["products"]
        picked = [products[(ordinal + slot) % len(products)] for slot in range(3)]
        items = []
        for slot, product in enumerate(picked):
            qty = _cases(product, year, month, slot)
            price = float(product.case_price or 0)
            items.append({
                "product_id": product.id, "sku": product.sku,
                "product_name": (product.display_name or product.sku)[:200],
                "quantity": qty, "case_price": price, "line_total": round(qty * price, 2),
                "case_pack": product.case_pack,
            })
        subtotal = round(sum(item["line_total"] for item in items), 2)
        terms = terms_cycle[ordinal % 3]
        status = _status(year, month)
        prepaid, balance = _money(status, terms, subtotal)
        submitted = datetime(year, month, 12, 2, 0, tzinfo=UTC)
        eta_month = month + 1 if month < 12 else 1
        eta_year = year if month < 12 else year + 1
        po_id = f"po-demo-{link['index']:02d}-{year}{month:02d}"
        db.insert_po(PurchaseOrder(
            id=po_id, po_number=f"PO-DEMO-{year}{month:02d}-{link['index']:02d}",
            brand_id=link["brand_id"], distributor_id=DIST_ID, partnership_id=link["partnership_id"],
            status=status, currency=link["currency"], payment_terms=terms, incoterms="FOB",
            shipping_method="sea", eta_date=f"{eta_year}-{eta_month:02d}-20", ship_to_country="CN",
            notes=None, discount_amount=None, discount_percentage=None,
            subtotal_amount=subtotal, total_amount=subtotal,
            submitted_at=submitted, submit_cycle_version=1,
            prepaid_amount=prepaid, balance_paid=balance,
        ), items, [])
        db.update_po(po_id, created_at=submitted)
        n += 1
    return n


def _seed_sales(db: Database, link: dict) -> int:
    facts_n = 0
    for ordinal, (year, month) in enumerate(months()):
        products = link["products"]
        picked = [products[(ordinal + slot) % len(products)] for slot in range(3)]
        start = f"{year}-{month:02d}-01"
        end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
        facts: list[ReportFact] = []
        row_no = 2
        for slot, product in enumerate(picked):
            qty = _cases(product, year, month, slot)
            pack = product.case_pack or 12
            sold_units = round(qty * pack * 0.78)
            unit_revenue = float(product.case_price or 0) / pack * 1.35
            for channel, share in CHANNELS:
                units = max(1, round(sold_units * share))
                facts.append(ReportFact(
                    metric_key="units_sold", fact_type="sales", channel=channel, sku=product.sku,
                    product_id=product.id, period_start=start, period_end=end,
                    numeric_value=units, unit="units", source_sheet="Sell-out",
                    source_row=row_no, source_col=4, source_header="Units",
                ))
                facts.append(ReportFact(
                    metric_key="revenue", fact_type="sales", channel=channel, sku=product.sku,
                    product_id=product.id, period_start=start, period_end=end,
                    numeric_value=round(units * unit_revenue, 2), currency=link["currency"],
                    source_sheet="Sell-out", source_row=row_no, source_col=5, source_header="Revenue",
                ))
                row_no += 1
            leftover = max(0, qty * pack - sold_units)
            facts.append(ReportFact(
                metric_key="stock_on_hand", fact_type="inventory", sku=product.sku,
                product_id=product.id, period_start=start, period_end=end,
                numeric_value=leftover, unit="units", source_sheet="Inventory",
                source_row=row_no, source_col=3, source_header="Stock",
            ))
        report_id = f"rpt-demo-{link['index']:02d}-{year}{month:02d}"
        db.save_report(SalesReport(
            id=report_id, brand_id=link["brand_id"], partnership_id=link["partnership_id"],
            title=f"Sell-out {year}-{month:02d}", file_name=f"sellout-{year}-{month:02d}.csv",
            media_type="text/csv", reporting_period=f"{year}-{month:02d}",
            period_start=start, period_end=end, notes="Demo sell-out, confirmed for report tests.",
        ), method="manual", model=None, raw={"source": "scripts/seed_demo.py"}, skipped=[], facts=facts)
        db.confirm_report(report_id)
        facts_n += len(facts)
    return facts_n


def _write_contract(link: dict) -> None:
    """The demo agreement. Headings stay splittable on 第N条; today's order is container 1."""
    products = link["products"][:4]
    prices = "\n".join(
        f"{product.sku}  {product.display_name}  每箱 {link['currency']} {float(product.case_price or 0):.2f}，单位为 per case。"
        for product in products
    )
    moqs = "\n".join(
        f"{product.sku} 的最低订货量为 {product.moq_units or (product.case_pack or 12)} units，按每张采购单、该 SKU 计算。"
        for product in products
    )
    body = (ROOT / "fixtures" / "demo" / "联采贸易-经销协议.md").read_text(encoding="utf-8")
    brand = link["brand_name"]
    body = body.replace("甲方 Activia", f"甲方 {brand}", 1)
    body = body.replace("甲方（签字）：Activia", f"甲方（签字）：{brand}", 1)
    body = body.replace("每箱 USD ", f"每箱 {link['currency']} ")
    body = body.replace("币种为 USD", f"币种为 {link['currency']}")
    body = body.replace("币种 USD", f"币种 {link['currency']}")
    body = body.replace("1200000 USD", f"1200000 {link['currency']}")
    old_prices = "\n".join(
        line for line in body.splitlines() if line.startswith("DAI-") and "每箱" in line
    )
    old_moqs = "\n".join(
        line for line in body.splitlines() if "最低订货量为" in line
    )
    body = body.replace(old_prices, prices).replace(old_moqs, moqs)
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(body, encoding="utf-8")


def _priced(db: Database, brand_id: str) -> int:
    with db.engine.connect() as conn:
        return int(conn.execute(text(
            """
            SELECT count(*) FROM products
            WHERE brand_id = :b AND case_price > 0 AND product_status = '正品'
            """
        ), {"b": brand_id}).scalar() or 0)


def _brand_row(db: Database, brand_id: str) -> tuple[str, str] | None:
    with db.engine.connect() as conn:
        row = conn.execute(text(
            "SELECT name, preferred_currency FROM brands WHERE id = :b"
        ), {"b": brand_id}).one_or_none()
    if row is None:
        return None
    return str(row.name), str(row.preferred_currency or "USD")


def _one_brand(tag: str) -> dict | None:
    """Up to eight regular products for one Open Food Facts brand tag."""
    url = (
        "https://world.openfoodfacts.org/api/v2/search?brands_tags="
        f"{tag}&page_size=24&fields=code,product_name,generic_name,brands,quantity,"
        "product_quantity,product_quantity_unit,categories_tags,labels_tags,obsolete"
    )
    payload = _get(url)
    products: list[dict] = []
    spellings: Counter = Counter()
    dropped: Counter = Counter()
    for raw in payload.get("products") or []:
        row = _off_row(raw, tag)
        derived = build_catalog.product(row, dropped)
        if not derived or derived["product_status"] != "正品":
            continue
        products.append(derived)
        spellings[row["brand"]] += 1
        if len(products) >= 8:
            break
    if len(products) < 4:
        print(f"  skip {tag}: only {len(products)} usable products")
        return None
    name = spellings.most_common(1)[0][0] if spellings else tag
    return {"id": build_catalog.brand_id(tag), "name": name[:200], "products": products}


def _store_brand(db: Database, brand: dict) -> None:
    db.upsert_brand(brand["id"], brand["name"], "USD")
    db.upsert_products(brand["id"], [
        Product(**{k: p[k] for k in Product.model_fields if k in p}) for p in brand["products"]
    ])


def _ensure_brand(db: Database, tag: str) -> tuple[str, str, str]:
    brand_id = build_catalog.brand_id(tag)
    if _priced(db, brand_id) < 4:
        print(f"loading {tag} from Open Food Facts")
        loaded = _one_brand(tag)
        if loaded is None:
            raise SystemExit(f"could not load four priced products for {tag}")
        _store_brand(db, loaded)
        brand_id = loaded["id"]
    found = _brand_row(db, brand_id)
    if found is None or _priced(db, brand_id) < 4:
        raise SystemExit(f"{tag} is still not a usable brand")
    return brand_id, found[0], found[1]


def ensure_accounts(db: Database) -> None:
    """海纳分销, and three people on each distributor. Existing chats stay with 陈思远."""
    db.upsert_distributor(DIST_ID, DIST_NAME, "CN")
    for user_id, name in LIANCAI_USERS:
        db.upsert_user(user_id, DIST_ID, name)
    liancai = {row.brand_id for row in _existing_partnerships(db)}
    db.upsert_distributor(DIST2_ID, DIST2_NAME, "CN")
    chosen: list[str] = []
    for tag in (*SHARED_TAGS, *EXTRA_TAGS):
        brand_id, name, currency = _ensure_brand(db, tag)
        if tag in EXTRA_TAGS and brand_id in liancai:
            raise SystemExit(f"{name} is already a 联采贸易 brand")
        pid = f"ps-haina-{tag}"
        db.upsert_partnership(Partnership(
            id=pid, brand_id=brand_id, distributor_id=DIST2_ID,
            brand_name=name, distributor_name=DIST2_NAME,
            ship_to_country="CN", currency=currency,
        ))
        chosen.append(name)
        print(f"  {DIST2_NAME} · {name}  {pid}")
    for user_id, name in HAINA_USERS:
        db.upsert_user(user_id, DIST2_ID, name)
    with db.engine.begin() as conn:
        conn.execute(text(
            """
            UPDATE sessions SET user_id = :user
            WHERE user_id IS NULL
              AND partnership_id IN (SELECT id FROM partnerships WHERE distributor_id = :dist)
            """
        ), {"user": LIANCAI_USERS[0][0], "dist": DIST_ID})
        conn.execute(text(
            """
            UPDATE memories SET user_id = :user
            WHERE user_id IS NULL
              AND partnership_id IN (SELECT id FROM partnerships WHERE distributor_id = :dist)
            """
        ), {"user": LIANCAI_USERS[0][0], "dist": DIST_ID})
    print(f"people: {', '.join(name for _, name in LIANCAI_USERS)} ({DIST_NAME})")
    print(f"people: {', '.join(name for _, name in HAINA_USERS)} ({DIST2_NAME})")
    print(f"{DIST2_NAME} brands: {', '.join(chosen)}")


def main() -> int:
    db = Database()
    brands = ensure_catalog(db)
    links = ensure_partnerships(db, brands)
    print(f"distributor {DIST_NAME} ({DIST_ID}), {len(links)} partnerships")
    _clear(db)
    orders = facts = 0
    for link in links:
        if len(link["products"]) < 3:
            raise SystemExit(f"{link['brand_name']} has fewer than 3 priced products")
        orders += _seed_orders(db, link)
        facts += _seed_sales(db, link)
        print(f"  {link['brand_name']}  {link['partnership_id']}")
    _write_contract(links[0])
    print(f"orders {orders} (none in 2026-10), confirmed sell-out facts {facts}")
    print(f"contract for {links[0]['brand_name']} ({links[0]['partnership_id']}): {CONTRACT_PATH.relative_to(ROOT)}")
    print("In the desk, pick that partnership and upload the contract. The October order is container 1 (3%).")
    ensure_accounts(db)
    return 0


if __name__ == "__main__":
    if "--accounts" in sys.argv:
        ensure_accounts(Database())
        raise SystemExit(0)
    raise SystemExit(main())
