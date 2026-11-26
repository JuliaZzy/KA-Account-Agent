"""The process-wide MCP loop is created once."""

from __future__ import annotations

import threading

from backend.mcp.runtime import McpHub


def test_two_callers_share_one_mcp_loop():
    hub = McpHub(configs={})
    found: list[object] = []

    def grab() -> None:
        found.append(hub._ensure_loop())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert len(found) == 8
        assert len({id(loop) for loop in found}) == 1
        assert hub._loop is not None and hub._loop.is_running()
    finally:
        hub.close()
