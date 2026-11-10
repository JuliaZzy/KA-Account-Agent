"""Chat turns enter the queue. The worker claims and runs them.

`enqueue_chat_run` writes the queued row and the outbox, then dispatches.
It does not claim the run. Closing the HTTP stream is not a cancel.

A chat parent run plans, parks while children run, then answers. Child runs
execute one planned tool and write the wave box. `docs/orchestrator.md`.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..db import Database
from ..llm.routing import run_budget_for
from ..repositories.run_repository import (
    RunStatus,
    create_agent_run,
    find_agent_run_by_id,
    find_runs_by_owner,
    latest_checkpoint,
)
from .dispatcher import dispatch_pending
from .run_names import RunType, ToolCallStatus
from .run_worker import PermanentRunError, sweep_expired_leases, work_once
from .session_title import apply_provisional_title


def enqueue_chat_run(
    db: Database,
    session: dict[str, Any],
    message: str,
    *,
    images: list[str] | None = None,
    documents: list[tuple[str, bytes]] | None = None,
    model: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    """Create through the outbox and dispatch. Do not claim the run.

    A missing session title is filled from the first message immediately. The
    OPEN refine run is created only after this chat reaches a terminal status.
    """
    # 鉴权
    user_id = str(session.get("user_id") or "")
    if not user_id:
        raise ValueError("session has no user_id")
    partnership = db.partnership(str(session["partnership_id"]))
    if partnership is None:
        raise ValueError("session has no partnership")

    # session id
    key = f"chat:{session['id']}:{uuid.uuid4().hex}"
    filed = [
        {"name": name, "data": base64.b64encode(data).decode()}
        for name, data in (documents or [])
        if name and data
    ]
    attachments = [name for name in (attachments or []) if isinstance(name, str)]
    apply_provisional_title(db, session, message, attachments)

    # 创建 run
    chat_budget = run_budget_for(RunType.CHAT)
    run, _created = create_agent_run(
        db, partnership.distributor_id, user_id, RunType.CHAT, key, {
            "session_id": session["id"],
            "message": (message or "")[:4000],
            "images": [url for url in (images or []) if isinstance(url, str)],
            "documents": filed,
            "model": (model or "").strip() or None,
            "attachments": attachments,
        },
        max_model_calls=chat_budget.max_model_calls,
        max_tokens=chat_budget.max_tokens,
    )
    dispatch_pending(db)
    return run


def work_chat(db: Database, llm: Any, run_id: str | None = None) -> bool:
    """Drain one chat turn for tests: parent, then children, then the parent again.

    Compose delivers each `run_id` to `work_once` separately. A `running` row whose
    lease has already expired is handed to the same sweeper the worker uses, then
    drained again, so a lost worker resumes through that requeue instead of stopping
    on `running`.
    """
    if not run_id:
        return False
    execute = execute_chat(db, llm)
    for _ in range(32):
        row = find_agent_run_by_id(db, run_id)
        if row is None:
            return False
        status = row["status"]
        if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.WAITING_FOR_USER):
            children = _queued_descendants(db, run_id)
            if not children:
                if _release_expired(db):
                    continue
                return True
            stuck = False
            for child in children:
                work_once(db, "test-worker", execute, run_id=child["id"])
                again = find_agent_run_by_id(db, child["id"])
                if again is not None and again["status"] == RunStatus.QUEUED:
                    stuck = True
            if stuck:
                return True
            continue
        if status == RunStatus.QUEUED:
            work_once(db, "test-worker", execute, run_id=run_id)
            again = find_agent_run_by_id(db, run_id)
            if again is not None and again["status"] == RunStatus.QUEUED:
                return True
            continue
        if status == RunStatus.WAITING_FOR_TASKS:
            children = _queued_descendants(db, run_id)
            if not children:
                if _release_expired(db):
                    continue
                return True
            for child in children:
                work_once(db, "test-worker", execute, run_id=child["id"])
            continue
        if status == RunStatus.RUNNING and _release_expired(db):
            continue
        return True
    return True


def _release_expired(db: Database) -> bool:
    """Requeue expired leases and dispatch the outbox the sweeper just wrote.

    `sweeper.sweep` does both. Skipping the dispatch leaves `run.enqueued` until a
    later turn, so a drain resume would not match a Celery redelivery.
    """
    released = sweep_expired_leases(db)
    if released:
        dispatch_pending(db)
    return released > 0


def _queued_descendants(db: Database, root_id: str) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    seen = {root_id}
    stack = [root_id]
    while stack:
        owner = stack.pop()
        for child in find_runs_by_owner(db, owner):
            child_id = str(child["id"])
            if child_id in seen:
                continue
            seen.add(child_id)
            stack.append(child_id)
            if child["status"] == RunStatus.QUEUED:
                pending.append(child)
    return pending


def execute_chat(db: Database, llm: Any | None = None) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The callable `work_once` takes. Chat parents plan; other run types are one task.

    None opens the vendor named by each call. Tests pass FakeLLM.
    """
    from ..orchestrator.gather import load_state
    from ..orchestrator.parent import execute_parent
    from ..orchestrator.speak import save_reply
    from ..turn_lock import hold_session
    from .task_run import execute_task

    def execute(run: dict[str, Any]) -> dict[str, Any]:
        # 如果有 open 类型：写 session 标题，打 tag
        if run.get("run_type") == RunType.OPEN:
            from ..orchestrator.turn import execute_open

            return execute_open(db, llm, run)

        # 有子任务，执行 execute_task
        if run.get("run_type") != RunType.CHAT or run.get("owner_run_id"):
            return execute_task(db, llm, run)

        payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}

        # 顶层 chat
        ## 检测输入，后续会加一个规则检测 & 重试机制
        session_id = str(payload.get("session_id") or "")
        session = db.session(session_id)
        if not session:
            raise ValueError("chat run has no session")
        text = str(payload.get("message") or "")
        ## 主管线
        from ..orchestrator.plan import InvalidPlan

        try:
            output = execute_parent(db, llm, run, session)
        except InvalidPlan as exc:
            raise PermanentRunError(str(exc)) from exc

        ## 判断是否需要写回复
        row = find_agent_run_by_id(db, run["id"]) # 重查数据库状态，而不是直接使用 output 的 status
        if output is None or (row is not None and row["status"] in (
            RunStatus.WAITING_FOR_USER, RunStatus.WAITING_FOR_TASKS,
        )):
            return output or {}

        ## 汇总子任务结果
        ### 从 checkpoint 再读一遍本轮所有子任务的结果
        state = load_state(latest_checkpoint(db, run["id"]))   
        raw = (state or {}).get("results") or {}
        results = list(raw.values()) if isinstance(raw, dict) else list(raw)
        ### 把结果映射成 desk 格式
        extra, fields, _side = desk_from_results(results)
        ### 取出 Answer 回复的内容
        reply = str((output or {}).get("text") or "").strip() or "How can I help?"

        ## 持锁写回复
        with hold_session(session_id):
            session = db.session(session_id) or session
            save_reply(db, session, text, reply, extra, llm=llm, session_fields=fields, run_id=run["id"])
        return {**output, "text": reply}

    return execute


def desk_from_tools(
    db: Database, session: dict[str, Any], run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Copy the last tool results onto the session and the desk events."""
    extra: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    side: list[dict[str, Any]] = []
    partnership = db.partnership(str(session["partnership_id"]))
    org_id = partnership.distributor_id if partnership is not None else ""
    trace = (db.run_trace(org_id, run_id) or {}) if org_id else {}
    for call in trace.get("tool_calls") or []:
        if call.get("status") != ToolCallStatus.SUCCEEDED:
            continue
        name = call.get("tool_name")
        payload = call.get("result_json") or {}
        if not isinstance(payload, dict):
            continue
        _apply_desk_payload(name, payload, extra, fields, side)
    return extra, fields, side


def desk_from_results(
    results: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Map planned-task payloads the same way `desk_from_tools` maps tool_calls."""
    extra: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    side: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        name = str(item.get("name") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        _apply_desk_payload(name, payload, extra, fields, side)
    return extra, fields, side


def _apply_desk_payload(
    name: Any, payload: dict[str, Any],
    extra: dict[str, Any], fields: dict[str, Any], side: list[dict[str, Any]],
) -> None:
    if name == "po.draft" and "slots" in payload:
        expires = payload.get("confirm_expires_at")
        when = datetime.fromisoformat(expires) if isinstance(expires, str) and expires else None
        fields.update({
            "slots": payload.get("slots") or {},
            "stage": payload.get("stage") or "gathering",
            "specialist": "po",
            "confirm_token": payload.get("confirm_token"),
            "confirm_hash": payload.get("confirm_hash"),
            "confirm_expires_at": when,
        })
        if payload.get("clear_po"):
            fields["po_id"] = None
        extra.update({
            "draft": payload.get("draft"), "order_draft": payload.get("draft"), "order": True,
            "stage": payload.get("stage"), "slots": payload.get("slots"),
            "gaps": payload.get("gaps") or [], "gaps_detail": payload.get("gaps_detail") or [],
            "advisories": payload.get("advisories") or [],
            "confirm_token": payload.get("confirm_token"),
        })
    if name == "po.draft" and payload.get("po_event"):
        extra["po_event"] = payload["po_event"]
    if name == "contract.read":
        side.append({
            "type": "contract", "status": payload.get("status"),
            "contract_id": payload.get("contract_id"), "table": payload.get("table"),
        })
        if isinstance(payload.get("table"), dict):
            extra["contract"] = payload["table"]
        if isinstance(payload.get("pending"), dict):
            extra["contract_pending"] = payload["pending"]
    if name == "report.draft" and isinstance(payload.get("data_query"), dict):
        extra["data_query"] = payload["data_query"]
    if name == "report.draft" and payload.get("ok"):
        extra["report_draft"] = True
        extra["report_pending_id"] = payload.get("report_pending_id")
        extra["report_body"] = payload.get("text") or ""
        if isinstance(payload.get("title"), str) and payload["title"].strip():
            extra["report_title"] = payload["title"].strip()
        if payload.get("charts"):
            extra["charts"] = payload["charts"]
        if payload.get("web_notes"):
            extra["web_notes"] = payload["web_notes"]
        for warning in payload.get("warnings") or []:
            side.append({"type": "warning", "message": warning})
    if name == "data.query":
        side.append({
            "type": "data", "ok": bool(payload.get("ok")),
            "row_count": payload.get("row_count") or 0, "log_id": payload.get("log_id"),
        })
        if payload.get("log_id"):
            extra["data_query"] = {
                "ok": bool(payload.get("ok")), "row_count": payload.get("row_count") or 0,
                "error": payload.get("error"), "log_id": payload["log_id"],
            }
        if payload.get("handoff"):
            side.append({"type": "warning", "message": payload["handoff"]})
    if name == "brand.search":
        hits = payload.get("hits")
        if isinstance(hits, list) and hits:
            extra["brand_hits"] = [
                {
                    "n": hit.get("n"), "cid": hit.get("cid"), "doc": hit.get("doc"), "page": hit.get("page"),
                    "file_name": hit.get("file_name"), "source": hit.get("source"),
                }
                for hit in hits if isinstance(hit, dict)
            ]

