"""代码检查的确定性搜索对不上时，用模型做语义回退。

词面重叠能抓住完全一致的 SKU、共有的词和规格，但跨不过意思：英文 “essence water”
对上只有中文名的精华水，或 “the anti-aging serum”。某个指称的搜索返回零条时，问一次模型——
每一轮只问一次，把所有没解析的指称放在一起——让它把每一个映射到最可能的 SKU。
它绝不编造 SKU：调用方只保留目录里真实存在的。

提示词里没有同义词表。模型根据完整的产品属性来判断。
"""

from __future__ import annotations

import json
from typing import Any

from ..llm import calls as model_calls


def build_resolver_prompt(references: list[str], products: list[dict[str, str]]) -> str:
    catalog = "\n".join(
        f"{p['sku']} | {' · '.join(x for x in (p['name'], p['native'], p['unit_size'], p['variant'], p['category']) if x)}"
        for p in products
    )
    refs = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(references))
    shape = ", ".join(f'{json.dumps(r, ensure_ascii=False)}: ["SKU", ...]' for r in references)
    return f"""你把经销商对产品的非正式说法对应到目录 SKU。综合全部属性来判断（英文名、本地名、规格、变体、品类），并且跨语言——一句英语可以指一款只有中文名的产品，反过来也一样。要考虑规格（“100ml”）和变体（“single”/“单支” 相对 “double”/“双支”）。

=== 目录（SKU | 名称 · 本地名 · 规格 · 变体 · 品类） ===
{catalog}

=== 待解析的说法 ===
{refs}

对每一个说法，返回它最可能指的 SKU：
- 恰好一个明确匹配 → 一个 SKU
- 几个都说得通（例如没说规格或变体） → 列出来，最好的放前面
- 目录里没有合适的 → 空数组
绝不要编造 SKU——只用上面目录里的。不要硬配。

只返回一个原始 JSON 对象，把每个说法原文映射到 SKU 数组：
{{ {shape} }}"""


def resolve_references(llm: Any, references: list[str], products: list[dict[str, str]]) -> dict[str, list[str]]:
    """reference (verbatim) → ordered candidate SKUs (may be empty). Raises on model
    failure — the caller leaves the reference unresolved."""
    if not references or not products:
        return {}
    raw = model_calls.school_json(
        model_calls.SCHOOL_CHAT,
        client=llm,
        system=build_resolver_prompt(references, products),
        messages=[{"role": "user", "content": "请解析上面的说法。"}],
        temperature=0,
    )
    out: dict[str, list[str]] = {}
    for ref in references:
        v = raw.get(ref)
        out[ref] = [s.strip() for s in v if isinstance(s, str) and s.strip()] if isinstance(v, list) else []
    return out
