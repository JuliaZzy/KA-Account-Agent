"""允许 text-to-SQL 模型看到的结构。

目录是一行一个表（用来选表）。列的细节只发给这个问题需要的表，再加上不连上就读不了这些表的父表。

不包含 `sessions`：里面有确认令牌、草稿槽位和聊天记录。
不包含 `users`：谁在用工作台不是业务数据。
不包含 `text2sql_logs`，这样查询不能读到或改写自己的审计。
不包含 `narrative_reports`：那是确认后的叙述正文，不是可以相加的数字。动销仍在 `report_facts`。
不包含 `brand_documents.file_bytes`；页面文本在 `brand_document_pages`。

每个视图已经滤到当前合作关系；目录和品牌材料则滤到这个品牌。模型不要再自己加合作关系条件。
"""

from __future__ import annotations

from typing import Any

# Tables that exist in the database and must never be queried from here.
EXCLUDED_TABLES = frozenset({
    "sessions", "text2sql_logs", "narrative_reports", "users",
    "agent_runs", "agent_steps", "run_events", "checkpoints", "usage_records",
    "model_calls", "tool_calls", "audit_logs", "outbox_jobs", "dead_letter_jobs",
})
# Columns that exist on a queryable table but are not in its view.
# `memories.user_id` is applied in the view, so one person's notes never appear for another.
HIDDEN_COLUMNS = {
    "brand_documents": frozenset({"file_bytes"}),
    "memories": frozenset({"user_id"}),
}

MAX_TABLES = 8


def _col(type_: str, description: str, examples: list[Any] | None = None) -> dict[str, Any]:
    column = {"column_type": type_, "column_description": description}
    if examples is not None:
        column["examples"] = examples
    return column


def _table(
    description: str,
    columns: dict[str, dict[str, Any]],
    *,
    where: str,
    joins: list[str] | None = None,
    source: str | None = None,
    alias: str | None = None,
) -> dict[str, Any]:
    return {
        "description": description,
        "columns": columns,
        "where": where,
        "joins": joins or [],
        "from": source,
        "alias": alias,
    }


_PARTNER = "partnership_id = :partnership_id"
_BRAND = "brand_id = :brand_id"

# Join a child table back to the partnership through a parent that carries the scope key.
_PO_CHILD = "public.{table} AS t JOIN public.purchase_orders AS po ON po.id = t.po_id"
_CONTRACT_CHILD = "public.{table} AS t JOIN public.contracts AS c ON c.id = t.contract_id"
_REPORT_CHILD = "public.{table} AS t JOIN public.sales_reports AS r ON r.id = t.report_id"


TABLES: dict[str, dict[str, Any]] = {
    "brands": _table(
        "这家经销商合作的品牌。一行。",
        {
            "id": _col("text", "主键。"),
            "name": _col("text", "品牌名称。", ["Northwind Foods"]),
            "preferred_currency": _col("text", "品牌定价使用的 ISO 货币。", ["USD", "EUR", "CNY"]),
            "created_at": _col("timestamptz", "行的创建时间，UTC。"),
            "updated_at": _col("timestamptz", "行的最后更新时间，UTC。"),
        },
        where="id = :brand_id",
    ),
    "distributors": _table(
        "这家经销商。一行。",
        {
            "id": _col("text", "主键。"),
            "name": _col("text", "经销商法定名称。"),
            "country": _col("text", "ISO 3166-1 alpha-2 国家代码，或 null。", ["US", "DE", "CN"]),
            "created_at": _col("timestamptz", "行的创建时间，UTC。"),
            "updated_at": _col("timestamptz", "行的最后更新时间，UTC。"),
        },
        where="id = :distributor_id",
    ),
    "partnerships": _table(
        "这场对话所属的品牌与经销商关系。一行。货币和收货地在这里。",
        {
            "id": _col("text", "主键。当前这个合作关系。"),
            "brand_id": _col("text", "对应 brands.id。"),
            "distributor_id": _col("text", "对应 distributors.id。"),
            "ship_to_country": _col("text", "默认收货国家，ISO alpha-2。", ["US"]),
            "currency": _col("text", "这个关系使用的货币。", ["USD"]),
            "created_at": _col("timestamptz", "关系的创建时间，UTC。"),
            "updated_at": _col("timestamptz", "关系的最后更新时间，UTC。"),
        },
        where="id = :partnership_id",
        joins=["brands", "distributors"],
    ),
    "products": _table(
        "这个品牌的产品目录。很大（几十万行）。按 sku 或 name 筛选，或者做聚合。"
        "不要把目录列出来。case_price 是每箱价格；moq_units 是件数，不是箱数。",
        {
            "id": _col("text", "主键。"),
            "brand_id": _col("text", "对应 brands.id。"),
            "sku": _col("text", "库存单位，同一品牌内唯一。", ["FO-100"]),
            "product_name_en": _col("text", "英文产品名。"),
            "product_name_native": _col("text", "标签所用语言的名称。英文标签上与英文名相同。"),
            "category": _col("text", "品类。"),
            "subcategory": _col("text", "子品类。"),
            "variant": _col("text", "变体，例如口味或色号。"),
            "form": _col("text", "物理形态，例如胶囊或瓶装。"),
            "unit_size": _col("text", "标签上的销售规格。", ["100 capsules"]),
            "net_content": _col("text", "标签上的净含量。", ["500 ml"]),
            "description_en": _col("text", "英文描述。很长；除非被问到，否则不要选出。"),
            "case_pack": _col("integer", "每箱件数。"),
            "case_price": _col("numeric", "一箱的价格，品牌货币。未定价时为 null。"),
            "moq_units": _col("integer", "起订量，单位是件，不是箱。"),
            "cases_per_layer": _col("integer", "一层托盘上的箱数。"),
            "layers_per_pallet": _col("integer", "一托上的层数。"),
            "cases_per_pallet": _col("integer", "一整托的箱数。"),
            "case_weight": _col("numeric", "一箱的重量。"),
            "product_status": _col("text", "商业状态。", ["正品", "赠品", "样品", "停产"]),
            "policy_tags": _col("jsonb", "政策标签字符串的列表。"),
            "restricted_territories": _col("jsonb", "产品不能发往的 ISO 国家代码列表。常常为空。"),
            "created_at": _col("timestamptz", "行的创建时间，UTC。"),
            "updated_at": _col("timestamptz", "行的最后更新时间，UTC。"),
        },
        where=_BRAND,
    ),
    "commercial_rules": _table(
        "约束采购单的品牌商业规则。rule_config 的形状取决于 rule_type。"
        "contract_id 为 null 表示这条规则不是从协议推导出来的。",
        {
            "id": _col("text", "主键。"),
            "brand_id": _col("text", "对应 brands.id。"),
            "contract_id": _col("text", "规则来自协议时为 contracts.id，否则为 null。"),
            "name": _col("text", "给人看的短名称。"),
            "rule_type": _col(
                "text", "这是哪一种检查。",
                ["min_order_value", "gift_pct_cap", "territory_restriction", "discount_approval", "unit_cap", "value_pct_cap"],
            ),
            "rule_config": _col("jsonb", "rule_type 的参数。不要编造键；把这个对象选出来。"),
            "severity": _col("text", "block 会拦住订单；advisory 不会。", ["block", "advisory"]),
            "is_active": _col("boolean", "未启用的规则不执行。"),
            "partner_scope": _col("text", "all，或一份列出的合作方。", ["all"]),
            "partner_ids": _col("jsonb", "partner_scope 不是 all 时的合作关系或经销商 id。"),
            "partner_regions": _col("jsonb", "规则适用的区域代码。"),
            "product_scope": _col("text", "all，或一份列出的产品。", ["all"]),
            "product_ids": _col("jsonb", "product_scope 不是 all 时的 products.id。"),
            "product_roles": _col("jsonb", "规则适用的产品角色。"),
            "product_tags": _col("jsonb", "规则适用的 policy_tags。"),
            "effective_from": _col("text", "起始日历日 YYYY-MM-DD，或 null。不是时间戳。"),
            "effective_until": _col("text", "结束日历日 YYYY-MM-DD，或 null。不是时间戳。"),
            "created_at": _col("timestamptz", "行的创建时间，UTC。"),
            "updated_at": _col("timestamptz", "行的最后更新时间，UTC。"),
        },
        where=_BRAND,
        joins=["contracts"],
    ),
    "brand_agent_settings": _table(
        "品牌工作台的默认商业条款和常驻说明。",
        {
            "brand_id": _col("text", "主键。对应 brands.id。"),
            "payment_terms": _col("text", "默认付款条件。", ["50_50", "100_prepaid", "net_30", "net_60"]),
            "incoterms": _col("text", "默认贸易术语。", ["FOB", "CIF", "EXW", "DDP", "DAP", "CFR"]),
            "shipping_method": _col("text", "默认运输方式。", ["sea", "air", "express", "land"]),
            "eta_days": _col("integer", "默认在途天数。"),
            "operating_context": _col("text", "常驻说明。很长；只有被问到品牌要求什么时才选出。"),
            "min_po_confidence": _col("numeric", "草稿可以确认之前，抽取置信度的下限。"),
            "tone": _col("text", "工作台说话的语气。"),
            "updated_at": _col("timestamptz", "设置的最后更新时间，UTC。"),
        },
        where=_BRAND,
    ),
    "contracts": _table(
        "为这个合作关系归档的协议。status 为 active 的是正在生效的；superseded 是更早的一份。"
        "期限日期是日历文本，不是时间戳。",
        {
            "id": _col("text", "主键。"),
            "brand_id": _col("text", "对应 brands.id。"),
            "partnership_id": _col("text", "对应 partnerships.id。"),
            "title": _col("text", "协议标题。"),
            "file_name": _col("text", "上传的文件名。"),
            "file_sha256": _col("text", "文件字节的哈希。回答里很少用得上。"),
            "contract_type": _col("text", "协议种类，按文件里读到的。"),
            "status": _col("text", "active 表示正在生效。", ["active", "superseded"]),
            "effective_from": _col("text", "协议生效的日历日 YYYY-MM-DD，或 null。"),
            "term_start_date": _col("text", "期限开始 YYYY-MM-DD，或 null。"),
            "term_end_date": _col("text", "期限结束 YYYY-MM-DD，或 null。"),
            "auto_renewal": _col("boolean", "期限是否自动续期。"),
            "exclusivity_type": _col("text", "原文所述的独家性，或 null。"),
            "territory_text": _col("text", "协议里的区域措辞。"),
            "annual_sales_target": _col("jsonb", "按原文读到的目标对象，或 null。"),
            "annual_sales_target_currency": _col("text", "年度目标的货币。", ["USD"]),
            "created_at": _col("timestamptz", "这一行归档的时间，UTC。"),
            "updated_at": _col("timestamptz", "行的最后更新时间，UTC。"),
        },
        where=_PARTNER,
        joins=["contract_discount_rules", "contract_territories"],
    ),
    "contract_extractions": _table(
        "合同阅读器的一次运行。raw 是逐字的 JSON。要数字时优先用规范化后的 contract_* 表。",
        {
            "id": _col("text", "主键。"),
            "contract_id": _col("text", "对应 contracts.id。"),
            "model": _col("text", "阅读这份协议的模型。"),
            "confidence_score": _col("numeric", "阅读器的置信度，0 到 1。"),
            "extraction_notes": _col("text", "阅读器留下的备注。"),
            "raw": _col("jsonb", "逐字抽取结果。很大。只有规范化的表不够用时才选出。"),
            "skipped": _col("jsonb", "映射器没能归位的字符串列表。"),
            "extracted_at": _col("timestamptz", "这次运行结束的时间，UTC。"),
        },
        where="c.partnership_id = :partnership_id",
        joins=["contracts"],
        source=_CONTRACT_CHILD.format(table="contract_extractions"),
        alias="t",
    ),
    "contract_discount_rules": _table(
        "协议上的一份折扣档期。档位在 contract_discount_tiers。"
        "applies_per 和 basis 说明这份档期在计什么。",
        {
            "id": _col("integer", "主键。"),
            "contract_id": _col("text", "对应 contracts.id。"),
            "position": _col("integer", "这份档期在协议里的顺序。"),
            "title": _col("text", "档期标题。"),
            "section_reference": _col("text", "条款或章节标签。"),
            "applies_per": _col("text", "档期按什么期间重置。", ["each_contract_year", "calendar_year", "per_order"]),
            "basis": _col("text", "计的是什么。", ["shipping_container_sequence", "purchase_volume"]),
            "source_clause_text": _col("text", "这份档期读自的条款。"),
        },
        where="c.partnership_id = :partnership_id",
        joins=["contracts", "contract_discount_tiers"],
        source=_CONTRACT_CHILD.format(table="contract_discount_rules"),
        alias="t",
    ),
    "contract_discount_tiers": _table(
        "折扣档期里的一个档位。to_container 为 null 表示这一档覆盖此后的每一个集装箱。"
        "discount_percent 为 0 表示目录价。",
        {
            "id": _col("integer", "主键。"),
            "rule_id": _col("integer", "对应 contract_discount_rules.id。"),
            "position": _col("integer", "档位的顺序。"),
            "from_container": _col("integer", "这一档覆盖的第一个集装箱，从 1 起。null 表示从第一个起。"),
            "to_container": _col("integer", "这一档覆盖的最后一个集装箱，含该箱。null 表示以及此后全部。"),
            "discount_percent": _col("numeric", "相对目录价的折扣百分比。0 表示目录价。"),
            "notes": _col("text", "档位备注。"),
        },
        where="c.partnership_id = :partnership_id",
        joins=["contract_discount_rules"],
        source=(
            "public.contract_discount_tiers AS t "
            "JOIN public.contract_discount_rules AS r ON r.id = t.rule_id "
            "JOIN public.contracts AS c ON c.id = r.contract_id"
        ),
        alias="t",
    ),
    "contract_moqs": _table(
        "协议里写明的最低数量。",
        {
            "id": _col("integer", "主键。"),
            "contract_id": _col("text", "对应 contracts.id。"),
            "quantity": _col("numeric", "最低数量。"),
            "unit": _col("text", "数量单位。", ["cases", "units"]),
            "applies_per": _col("text", "这个最低量适用于什么。", ["per_order", "per_year"]),
            "product_scope": _col("text", "哪些产品，按条款里的原文。"),
            "sku": _col("text", "最低量只针对一个产品时的 SKU。"),
            "currency": _col("text", "最低量是金额时的货币。"),
            "source_clause_text": _col("text", "这个最低量读自的条款。"),
        },
        where="c.partnership_id = :partnership_id",
        joins=["contracts"],
        source=_CONTRACT_CHILD.format(table="contract_moqs"),
        alias="t",
    ),
    "contract_price_list": _table(
        "协议里写明的价格。unit_price 是条款里的数字，不是算出来的箱价。",
        {
            "id": _col("integer", "主键。"),
            "contract_id": _col("text", "对应 contracts.id。"),
            "product_name": _col("text", "协议里写的产品名。"),
            "product_sku": _col("text", "协议里写的 SKU。"),
            "unit_price": _col("numeric", "条款里的价格。"),
            "currency": _col("text", "unit_price 的货币。", ["USD"]),
            "unit": _col("text", "unit_price 买的是什么单位。", ["case", "unit"]),
            "list_price_reference": _col("text", "这个价格指向哪一份价目。"),
            "product_specific_discount_text": _col("text", "针对该产品的折扣措辞。"),
            "source_clause_text": _col("text", "这个价格读自的条款。"),
        },
        where="c.partnership_id = :partnership_id",
        joins=["contracts"],
        source=_CONTRACT_CHILD.format(table="contract_price_list"),
        alias="t",
    ),
    "contract_territories": _table(
        "协议允许或排除的国家。",
        {
            "id": _col("integer", "主键。"),
            "contract_id": _col("text", "对应 contracts.id。"),
            "country_code": _col("text", "ISO alpha-2。", ["US", "DE"]),
            "kind": _col("text", "该国家是否在区域内。", ["allowed", "excluded"]),
        },
        where="c.partnership_id = :partnership_id",
        joins=["contracts"],
        source=_CONTRACT_CHILD.format(table="contract_territories"),
        alias="t",
    ),
    "contract_evidence": _table(
        "合同字段是从文件的哪里读到的。",
        {
            "id": _col("integer", "主键。"),
            "contract_id": _col("text", "对应 contracts.id。"),
            "field": _col("text", "这段引文支撑哪个合同字段。"),
            "page": _col("integer", "从 1 起的页码，或 null。"),
            "quote": _col("text", "引用的段落。"),
            "section_hint": _col("text", "章节标题，如果有。"),
            "char_start": _col("integer", "引文的字符偏移，或 null。"),
        },
        where="c.partnership_id = :partnership_id",
        joins=["contracts"],
        source=_CONTRACT_CHILD.format(table="contract_evidence"),
        alias="t",
    ),
    "contract_chunks": _table(
        "一份协议的标题树。body 是节的正文，可能很长；先按 title 或 path 筛选。",
        {
            "id": _col("integer", "主键。"),
            "contract_id": _col("text", "对应 contracts.id。"),
            "chunk_id": _col("text", "这个标题在协议内部的 id。"),
            "parent_chunk_id": _col("text", "父标题的 chunk_id，根节点为 null。"),
            "number": _col("text", "印在文件上的标题编号，例如 3.2。"),
            "title": _col("text", "标题。"),
            "body": _col("text", "节的正文。很长。选出之前先筛选。"),
            "path": _col("text", "祖先标题拼成的一条路径。"),
            "page_start": _col("integer", "这一节的起始页。"),
            "page_end": _col("integer", "这一节的结束页。"),
            "sibling_order": _col("integer", "同一父级下的顺序。"),
        },
        where="c.partnership_id = :partnership_id",
        joins=["contracts"],
        source=_CONTRACT_CHILD.format(table="contract_chunks"),
        alias="t",
    ),
    "purchase_orders": _table(
        "这个合作关系已提交的采购单。这张表上没有 quantity；行数量是 purchase_order_items 上的箱数。"
        "金额列已经算好；求和，不要重新计价。"
        "eta_date 是日历日。submitted_at 是 UTC 时刻。",
        {
            "id": _col("text", "主键。"),
            "po_number": _col("text", "给人看的订单号。", ["PO-20261019-0001"]),
            "brand_id": _col("text", "对应 brands.id。"),
            "distributor_id": _col("text", "对应 distributors.id。"),
            "partnership_id": _col("text", "对应 partnerships.id。"),
            "status": _col(
                "text", "订单履行到哪一步。",
                ["submitted", "awaiting_payment", "payment_claimed", "completed", "awaiting_refund", "claim_filed"],
            ),
            "currency": _col("text", "金额列的货币。", ["USD"]),
            "payment_terms": _col("text", "这张订单怎么付款。", ["50_50", "100_prepaid", "net_30", "net_60"]),
            "incoterms": _col("text", "贸易术语。", ["FOB", "CIF", "EXW", "DDP", "DAP", "CFR"]),
            "shipping_method": _col("text", "怎么运输。", ["sea", "air", "express", "land"]),
            "eta_date": _col("text", "预计到达 YYYY-MM-DD，或 null。不是时间戳。"),
            "ship_to_country": _col("text", "收货国家，ISO alpha-2。"),
            "notes": _col("text", "订单上的自由文本备注。"),
            "discount_amount": _col("numeric", "货币金额的折扣，没有时为 null。"),
            "discount_percentage": _col("numeric", "折扣百分比；折扣是金额或没有折扣时为 null。"),
            "subtotal_amount": _col("numeric", "折扣前各行合计之和。"),
            "total_amount": _col("numeric", "subtotal_amount 减去 discount_amount。"),
            "submitted_at": _col("timestamptz", "订单提交时间，UTC，或 null。"),
            "submit_cycle_version": _col("integer", "这张订单提交过几次。"),
            "prepaid_amount": _col("numeric", "记为已预付的金额。"),
            "balance_paid": _col("numeric", "交货后记为已付的尾款。"),
            "created_at": _col("timestamptz", "行的创建时间，UTC。"),
            "updated_at": _col("timestamptz", "行的最后更新时间，UTC。"),
        },
        where=_PARTNER,
        joins=["purchase_order_items"],
    ),
    "purchase_order_items": _table(
        "采购单的一行。quantity 是箱数，不是件数。line_total 已经是 quantity 乘 case_price。",
        {
            "id": _col("integer", "主键。"),
            "po_id": _col("text", "对应 purchase_orders.id。"),
            "product_id": _col("text", "对应 products.id。"),
            "sku": _col("text", "抄到这一行上的 SKU。"),
            "product_name": _col("text", "抄到这一行上的名称。"),
            "quantity": _col("integer", "箱数。"),
            "case_price": _col("numeric", "这一行一箱的价格。"),
            "line_total": _col("numeric", "quantity 乘 case_price。"),
            "case_pack": _col("integer", "下单时的每箱件数。"),
        },
        where="po.partnership_id = :partnership_id",
        joins=["purchase_orders"],
        source=_PO_CHILD.format(table="purchase_order_items"),
        alias="t",
    ),
    "po_rule_evaluations": _table(
        "提交时的规则结果快照。snapshot 是每条规则结果的 JSON 列表。",
        {
            "id": _col("integer", "主键。"),
            "po_id": _col("text", "对应 purchase_orders.id。"),
            "evaluation_context": _col("text", "快照是什么时候拍的。", ["submission"]),
            "submit_cycle_version": _col("integer", "这份快照属于第几次提交。"),
            "snapshot": _col("jsonb", "规则结果的列表。很大。"),
            "created_at": _col("timestamptz", "快照存下的时间，UTC。"),
        },
        where="po.partnership_id = :partnership_id",
        joins=["purchase_orders"],
        source=_PO_CHILD.format(table="po_rule_evaluations"),
        alias="t",
    ),
    "po_events": _table(
        "订单存在之后发生的事：提交、收货、付款、破损索赔。",
        {
            "id": _col("integer", "主键。"),
            "po_id": _col("text", "对应 purchase_orders.id。"),
            "kind": _col("text", "事件名称。", ["submitted", "received", "payment", "claim"]),
            "data": _col("jsonb", "事件载荷。键取决于 kind。"),
            "created_at": _col("timestamptz", "事件记下的时间，UTC。"),
        },
        where="po.partnership_id = :partnership_id",
        joins=["purchase_orders"],
        source=_PO_CHILD.format(table="po_events"),
        alias="t",
    ),
    "brand_documents": _table(
        "品牌上传的文件（演示文稿、目录）。这是文件记录，不是正文。"
        "页面文本在 brand_document_pages。原始文件字节不能查询。",
        {
            "id": _col("text", "主键。"),
            "brand_id": _col("text", "对应 brands.id。"),
            "title": _col("text", "标题，或 null。"),
            "file_name": _col("text", "上传的文件名。"),
            "file_sha256": _col("text", "文件字节的哈希。"),
            "media_type": _col("text", "MIME 类型。"),
            "size_bytes": _col("integer", "文件大小，字节。"),
            "page_count": _col("integer", "页数。"),
            "created_at": _col("timestamptz", "文件存下的时间，UTC。"),
            "updated_at": _col("timestamptz", "行的最后更新时间，UTC。"),
        },
        where=_BRAND,
        joins=["brand_document_pages"],
    ),
    "brand_document_pages": _table(
        "品牌文档某一页的文本。text 可能很长；按文档或页码筛选。",
        {
            "id": _col("integer", "主键。"),
            "document_id": _col("text", "对应 brand_documents.id。"),
            "page_number": _col("integer", "从 1 起的页码。"),
            "text": _col("text", "这一页的文本。很长。"),
        },
        where="d.brand_id = :brand_id",
        joins=["brand_documents"],
        source=(
            "public.brand_document_pages AS t "
            "JOIN public.brand_documents AS d ON d.id = t.document_id"
        ),
        alias="t",
    ),
    "sales_reports": _table(
        "这家经销商的动销或库存文件。status 为 confirmed 表示有人接受了它。"
        "期间列是日历文本（YYYY-MM-DD），不是时间戳。数字在 report_facts。",
        {
            "id": _col("text", "主键。"),
            "brand_id": _col("text", "对应 brands.id。"),
            "partnership_id": _col("text", "对应 partnerships.id。"),
            "title": _col("text", "报告标题。"),
            "file_name": _col("text", "上传的文件名。"),
            "file_sha256": _col("text", "文件字节的哈希。"),
            "media_type": _col("text", "MIME 类型。"),
            "size_bytes": _col("integer", "文件大小，字节。"),
            "reporting_period": _col("text", "期间标签。", ["2026-03", "2026-Q1", "2026"]),
            "period_start": _col("text", "起始日历日 YYYY-MM-DD，或 null。"),
            "period_end": _col("text", "结束日历日 YYYY-MM-DD，或 null。"),
            "status": _col("text", "人确认之前是 draft。", ["draft", "confirmed"]),
            "confirmed_at": _col("timestamptz", "确认时间，UTC，或 null。"),
            "notes": _col("text", "报告上的备注。"),
            "created_at": _col("timestamptz", "行的创建时间，UTC。"),
            "updated_at": _col("timestamptz", "行的最后更新时间，UTC。"),
        },
        where=_PARTNER,
        joins=["report_facts"],
    ),
    "report_extractions": _table(
        "一次销售报告阅读是怎么找到列的。数字在 report_facts，不在这里。",
        {
            "id": _col("text", "主键。"),
            "report_id": _col("text", "对应 sales_reports.id。"),
            "method": _col("text", "跑的是哪一种阅读器。", ["tidy_table", "ai_table_extractor", "manual"]),
            "model": _col("text", "模型定位列时的模型名。"),
            "raw": _col("jsonb", "列映射。很大。"),
            "skipped": _col("jsonb", "阅读器跳过的内容列表。"),
            "extracted_at": _col("timestamptz", "这次阅读结束的时间，UTC。"),
        },
        where="r.partnership_id = :partnership_id",
        joins=["sales_reports"],
        source=_REPORT_CHILD.format(table="report_extractions"),
        alias="t",
    ),
    "report_facts": _table(
        "销售报告里的一个数字。除非问题问的是草稿，否则优先 status 为 confirmed。"
        "superseded 的事实已被同一期间后来的报告替换；不要把它们和 confirmed 加在一起。"
        "period_start 和 period_end 是日历文本 YYYY-MM-DD。numeric_value 是这个数字。",
        {
            "id": _col("integer", "主键。"),
            "report_id": _col("text", "对应 sales_reports.id。"),
            "extraction_id": _col("text", "report_extractions.id，或 null。"),
            "metric_key": _col("text", "量的是什么。", ["units_sold", "revenue", "unit_price", "stock_on_hand"]),
            "fact_type": _col("text", "指标所属的一类。", ["sales", "inventory", "marketing", "narrative"]),
            "channel": _col("text", "销售渠道，或 null。"),
            "sku": _col("text", "这个数字对应的 SKU，或 null。"),
            "product_id": _col("text", "SKU 对上目录时为 products.id，否则为 null。"),
            "entity": _col("text", "其他主体，例如零售商名称。"),
            "period_start": _col("text", "起始日历日 YYYY-MM-DD，或 null。"),
            "period_end": _col("text", "结束日历日 YYYY-MM-DD，或 null。"),
            "numeric_value": _col("numeric", "这个数字。事实是文本时为 null。"),
            "text_value": _col("text", "文字事实；事实是数字时为 null。"),
            "unit": _col("text", "numeric_value 的单位。", ["units"]),
            "currency": _col("text", "metric_key 是金额时的货币。", ["USD"]),
            "source_sheet": _col("text", "单元格来自的工作表或页。"),
            "source_row": _col("integer", "源行号，从 1 起。"),
            "source_col": _col("integer", "源列号，从 1 起。"),
            "source_header": _col("text", "列标题，逐字。"),
            "ai_derived": _col("boolean", "数字是推导出来的、而不是从单元格读到的时候为 true。"),
            "status": _col("text", "confirmed 是正在生效的数字。", ["draft", "confirmed", "superseded"]),
            "created_at": _col("timestamptz", "这条事实存下的时间，UTC。"),
        },
        where="r.partnership_id = :partnership_id",
        joins=["sales_reports", "products"],
        source=_REPORT_CHILD.format(table="report_facts"),
        alias="t",
    ),
    "memories": _table(
        "这个人在这个合作关系下留下的备注：偏好、事实、指示、事件。"
        "只有 is_active 的行仍然有效。同事的备注不在这里。",
        {
            "id": _col("integer", "主键。更大表示更新。"),
            "partnership_id": _col("text", "对应 partnerships.id。"),
            "kind": _col("text", "是哪一种备注。", ["preference", "fact", "instruction", "event"]),
            "content": _col("text", "备注正文。"),
            "source": _col("text", "谁写的。", ["user", "chat", "system"]),
            "session_id": _col("text", "在哪场对话里记下的，或 null。"),
            "is_active": _col("boolean", "false 表示已忘掉。"),
            "last_recalled_at": _col("timestamptz", "上次展示给工作台的时间，UTC，或 null。"),
            "created_at": _col("timestamptz", "存下的时间，UTC。"),
            "updated_at": _col("timestamptz", "最后一次修改的时间，UTC。"),
        },
        where=f"{_PARTNER} AND (:user_id = 'all' OR user_id = :user_id)",
    ),
}


def catalog_text() -> str:
    """One line per table, for choosing which tables a question needs."""
    return "\n".join(f"- {name}: {meta['description']}" for name, meta in TABLES.items())


def expand_tables(names: list[str], *, limit: int = MAX_TABLES) -> list[str]:
    """Keep known tables, then add join parents until `limit`. Unknown names are dropped."""
    chosen: list[str] = []
    queue = [name for name in names if isinstance(name, str)]
    while queue and len(chosen) < limit:
        name = queue.pop(0)
        if name in chosen or name not in TABLES:
            continue
        chosen.append(name)
        for extra in TABLES[name]["joins"]:
            if extra not in chosen:
                queue.append(extra)
    return chosen


def describe_tables(names: list[str]) -> str:
    """Column-level description for the SQL prompt. Names must already be in TABLES."""
    blocks: list[str] = []
    for name in names:
        meta = TABLES[name]
        lines = [f"表 {name} — {meta['description']}"]
        if meta["joins"]:
            lines.append("  常与这些表连接：" + ", ".join(meta["joins"]))
        for column, info in meta["columns"].items():
            example = f" 示例：{info['examples']}" if "examples" in info else ""
            lines.append(f"  {column} {info['column_type']} — {info['column_description']}{example}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def scope_views() -> list[tuple[str, str]]:
    """TEMP view SQL. Each view reads public.* with bound scope ids, so the user statement never sees other rows."""
    views: list[tuple[str, str]] = []
    for name, meta in TABLES.items():
        alias = meta.get("alias")
        prefix = f"{alias}." if alias else ""
        columns = ", ".join(f"{prefix}{column}" for column in meta["columns"])
        source = meta.get("from") or f"public.{name}"
        views.append((name, f"SELECT {columns} FROM {source} WHERE {meta['where']}"))
    return views
