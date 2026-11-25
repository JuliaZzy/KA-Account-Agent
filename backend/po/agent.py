"""采购单代理。一轮聊天：对已提交订单的一则声明，或下单对话。

定价、起订量和商业规则留在 `check` 和 `rules`。这个模块只选择这条消息走哪条路。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..db import Database
from ..orchestrator.status import status
from .claims import handle_post_submit
from .turn import Bundle, order_result, prepare_order


async def collect_order(
    db: Database, llm: Any, session: dict[str, Any], text: str, images: list[str], bundle: Bundle,
    emit: Callable[[dict[str, Any]], None], run_id: str | None = None, step_id: str | None = None,
) -> dict[str, Any]:
    """Draft the order, or record a receipt, without writing the chat transcript.

    The orchestrator is the only writer of the session row when several tasks run together.
    """
    local = dict(session)
    if local.get("stage") == "submitted" and local.get("po_id"):
        po = db.po(local["po_id"])
        if po:
            emit(status("po-after", "售后", "正在看这是收货、付款还是破损", state="running"))
            outcome = await handle_post_submit(db, llm, po, text, run_id=run_id, step_id=step_id)
            if outcome.kind != "none":
                emit(status("po-after", "售后", "已记到订单上", state="done"))
                return {"note": outcome.message, "po_event": outcome.kind}
            if outcome.message:
                emit(status("po-after", "售后", "这一句先不改订单", state="done"))
                return {"note": outcome.message}
            emit(status("po-after", "售后", "开始一张新订单", state="done"))
        local["slots"] = {}
        local["stage"] = "gathering"
        prepared = await prepare_order(db, llm, bundle, local, text, images, run_id=run_id, step_id=step_id)
        for event in prepared.events:
            emit(event)
        return order_result(prepared, bundle, clear_po=True)
    prepared = await prepare_order(db, llm, bundle, local, text, images, run_id=run_id, step_id=step_id)
    for event in prepared.events:
        emit(event)
    return order_result(prepared, bundle)
