"""Public web search through the local Open-WebSearch daemon.

Compose runs `node build/index.js serve` and injects `WEB_SEARCH_URL`
(`http://web-search:3210`). This is the daemon HTTP API, not the image's
default MCP server. Engines stay Baidu and Bing; the query is sent as-is.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import env
from ..resilience import ProviderError, classify, classify_status

DEFAULT_URL = "http://web-search:3210"
ENGINES = ("baidu", "bing")
MAX_COUNT = 5
PROVIDER = "web-search"

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


def search(query: str, *, count: int = MAX_COUNT, base_url: str | None = None) -> list[dict[str, str]]:
    root = (base_url if base_url is not None else env("WEB_SEARCH_URL", DEFAULT_URL)).strip().rstrip("/")
    if not root:
        raise RuntimeError("WEB_SEARCH_URL is not set")
    limit = max(1, min(count, MAX_COUNT))
    body = json.dumps({
        "query": query,
        "limit": limit,
        "engines": list(ENGINES),
        "searchMode": "request",
    }).encode()
    req = urllib.request.Request(
        f"{root}/search", data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        kind, _retryable = classify_status(err.code)
        raise ProviderError(PROVIDER, "search", kind, status_code=err.code) from err
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ProviderError(PROVIDER, "search", "server_error")
    pages = (payload.get("data") or {}).get("results") or []
    out: list[dict[str, str]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        out.append({
            "title": str(page.get("title") or "").strip(),
            "snippet": str(page.get("description") or "").strip(),
            "url": str(page.get("url") or "").strip(),
        })
        if len(out) >= limit:
            break
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
