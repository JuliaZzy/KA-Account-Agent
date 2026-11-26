"""通过博查做公开网页检索。只给报告添一层只读的背景——不是我们账本的来源。

https://api.bocha.cn/v1/web-search  （`BOCHA_API_KEY`）
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import env
from ..llm.errors import ProviderError, classify, classify_status

BOCHA_URL = "https://api.bocha.cn/v1/web-search"
MAX_COUNT = 5

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "检索公开网页，查找模型还不知道的当前信息、新闻或报告。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
            },
            "required": ["query"],
        },
    },
}


def search(query: str, *, count: int = MAX_COUNT, api_key: str | None = None) -> list[dict[str, str]]:
    key = (api_key if api_key is not None else env("BOCHA_API_KEY")).strip()
    if not key:
        raise RuntimeError("BOCHA_API_KEY is not set")
    body = json.dumps({
        "query": query, "freshness": "noLimit", "summary": True, "count": max(1, min(count, MAX_COUNT)),
    }).encode()
    req = urllib.request.Request(
        BOCHA_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        kind, _retryable = classify_status(err.code)
        raise ProviderError("bocha", "web-search", kind, status_code=err.code) from err
    pages = (((payload.get("data") or {}).get("webPages") or {}).get("value")) or []
    out: list[dict[str, str]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        out.append({
            "title": str(page.get("name") or "").strip(),
            "snippet": str(page.get("snippet") or page.get("summary") or "").strip(),
            "url": str(page.get("url") or "").strip(),
        })
    return out


def run(arguments: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"ok": False, "data": None, "error": "query required"}
    try:
        return {"ok": True, "data": {"results": search(query)}, "error": None}
    except Exception as err:
        kind = classify(err)
        return {"ok": False, "data": None, "error": str(err), "code": kind.code}
