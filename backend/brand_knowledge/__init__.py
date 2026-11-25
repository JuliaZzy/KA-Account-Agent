"""品牌材料检索。

`retrieve` 是这个品牌的块上的混合检索，可选交叉编码器重排。`evaluate_retrieval` 给一次标注过的运行打分。
`answer` 把追问改写成可以单独检索的查询，并给命中的段落补上来源名。回答由编排器写。
"""

from __future__ import annotations

from .rag import (
    RagUnavailable,
    chunks_from_pages,
    evaluate_retrieval,
    evidence_prompt,
    index_brand,
    retrieve,
)

__all__ = [
    "RagUnavailable",
    "chunks_from_pages",
    "evaluate_retrieval",
    "evidence_prompt",
    "index_brand",
    "retrieve",
]
