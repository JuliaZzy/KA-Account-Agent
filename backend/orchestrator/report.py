"""Report child run: plan, enqueue data queries, resume, then finish the draft."""

from __future__ import annotations

import uuid
from typing import Any

from ..db import Database
from ..memory import actor_id, prompt_context, standing_block, summary_block
from ..report.turn import (
    ReportDraft,
    _plan,
    asks_from_payloads,
    iter_after_queries,
    planned_questions,
    planned_searches,
    searches_from_payloads,
)
from ..repositories.run_repository import latest_checkpoint
from ..services.run_names import StepStatus, StepType, ToolCallStatus, step_name
from .gather import checkpoint_state, collect_wave, dispatch_wave, finish_child, load_state
from .plan import PlanTask

REPORT_KEY = "report"


def execute_report(
    db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any],
) -> dict[str, Any] | None:
    """Advance one report claim. None means parked for nested data runs."""
    from ..llm.context import reset_call_context, set_call_context

    payload = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    text = str(payload.get("message") or payload.get("input", {}).get("message") or "")
    if isinstance(payload.get("input"), dict) and payload["input"].get("message"):
        text = str(payload["input"]["message"])
    asked_text = str((payload.get("input") or {}).get("question") or text)
    state = load_state(latest_checkpoint(db, run["id"]))
    report = (state or {}).get(REPORT_KEY) if isinstance(state, dict) else None
    step_id = str((report or {}).get("step_id") or "") or _open_step(db, run, payload)
    token = set_call_context(
        db=db, run_id=str(run["id"]), step_id=step_id or None, purpose="report",
    )
    try:
        return _advance_report(
            db, llm, run, session, payload, asked_text, state, report, step_id,
        )
    finally:
        reset_call_context(token)


def _advance_report(
    db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any],
    payload: dict[str, Any], text: str, state: dict[str, Any] | None,
    report: dict[str, Any] | None, step_id: str,
) -> dict[str, Any] | None:
    phase = str((report or {}).get("phase") or "")
    if phase == "done" and isinstance((report or {}).get("result"), dict):
        result = report["result"]
        finish_child(db, run, result)
        return result
    if phase == "planned":
        return _resume_decision(db, llm, run, session, payload, text, step_id, report or {}, "await_query")
    if not phase:
        return _start(
            db, llm, run, session, payload, text, step_id,
        )
    wave_id = state.get("wave_id") if isinstance(state, dict) else None
    results = dict((state or {}).get("results") or {})
    session_id = str(session["id"])
    if isinstance(wave_id, str) and wave_id:
        results.update(collect_wave(db, run, session_id, wave_id))
    if phase == "await_query":
        return _after_query(db, llm, run, session, payload, text, step_id, report or {}, results)
    return _fail(db, run, payload, "unknown report phase", step_id)


def _resume_decision(
    db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any],
    payload: dict[str, Any], text: str, step_id: str, report: dict[str, Any], next_phase: str,
) -> dict[str, Any] | None:
    """Dispatch the decision already stored. Do not call the planner again."""
    from ..report.turn import SectionPlan

    tasks: list[PlanTask] = []
    for item in report.get("planned") or []:
        if not isinstance(item, dict):
            continue
        tasks.append(PlanTask(
            id=str(item.get("id") or ""),
            kind=str(item.get("kind") or "agent"),
            name=str(item.get("name") or "data"),
            input=item.get("input") if isinstance(item.get("input"), dict) else {},
            depends_on=[str(dep) for dep in (item.get("depends_on") or []) if dep],
        ))
    if tasks:
        parked = dispatch_wave(db, run, session, tasks, [], {}, payload, extra={
            REPORT_KEY: {**report, "phase": next_phase},
        })
        if parked is not None:
            return None
    planned = [SectionPlan(**item) for item in report.get("sections") or [] if isinstance(item, dict)]
    empty = {section.id: [] for section in planned}
    return _consume(
        db, llm, run, session, payload, text, step_id, planned,
        str(report.get("title") or ""), [], empty, [], found=[],
    )


def _start(
    db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any],
    payload: dict[str, Any], text: str, step_id: str,
) -> dict[str, Any] | None:
    remembered = _remembered(db, session)
    planned, title = _plan(
        llm, message=text, history=list(session.get("history") or []),
        hint=None, remembered=remembered,
        folded_through=int(session.get("folded_through") or 0),
        trace=_trace(db, run["id"], step_id),
    )
    questions = planned_questions(planned)
    searches = planned_searches(planned)
    tasks, question_pairs, search_pairs = _first_wave(questions, searches)
    decision = {
        "phase": "planned",
        "step_id": step_id,
        "title": title,
        "planned": [task.as_dict() for task in tasks],
        "sections": [section.__dict__ for section in planned],
        "questions": question_pairs,
        "searches": search_pairs,
    }
    _close_decision(db, run["id"], "report.plan", decision, tasks)
    if not tasks:
        empty = {section.id: [] for section in planned}
        return _consume(
            db, llm, run, session, payload, text, step_id, planned, title, [], empty, [],
            found=[],
        )
    extra = {REPORT_KEY: {**decision, "phase": "await_query"}}
    parked = dispatch_wave(db, run, session, tasks, [], {}, payload, extra=extra)
    if parked is not None:
        return None
    empty = {section.id: [] for section in planned}
    return _consume(
        db, llm, run, session, payload, text, step_id, planned, title, [], empty, [],
        found=[],
    )


def _after_query(
    db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any],
    payload: dict[str, Any], text: str, step_id: str,
    report: dict[str, Any], results: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    from ..report.turn import SectionPlan

    planned = [SectionPlan(**item) for item in report.get("sections") or [] if isinstance(item, dict)]
    pairs = [
        (str(task_id), str(question))
        for task_id, question in report.get("questions") or []
        if task_id and question
    ]
    asked, by_section, failures = asks_from_payloads(planned, results, pairs)
    search_pairs = [
        (str(task_id), str(query))
        for task_id, query in report.get("searches") or []
        if task_id and query
    ]
    found = searches_from_payloads(search_pairs, results)
    return _consume(
        db, llm, run, session, payload, text, step_id, planned,
        str(report.get("title") or ""), asked, by_section, failures,
        found=found,
    )


def _consume(
    db: Database, llm: Any, run: dict[str, Any], session: dict[str, Any],
    payload: dict[str, Any], text: str, step_id: str,
    planned: Any, title: str, asked: list, by_section: dict, failures: list[str],
    *, found: list,
) -> dict[str, Any] | None:
    partnership = db.partnership(str(session["partnership_id"]))
    items = iter_after_queries(
        db, llm, planned=planned, report_title=title, asked=asked, by_section=by_section,
        failures=failures, message=text, history=list(session.get("history") or []),
        remembered=_remembered(db, session),
        folded_through=int(session.get("folded_through") or 0),
        partnership_id=str(session["partnership_id"]),
        brand_id=str(getattr(partnership, "brand_id", "") or ""),
        distributor_id=str(getattr(partnership, "distributor_id", "") or ""),
        session_id=str(session["id"]),
        trace=_trace(db, run["id"], step_id),
        found=found,
    )
    draft: ReportDraft | None = None
    for item in items:
        if isinstance(item, ReportDraft):
            draft = item
    return _done(db, run, payload, draft, step_id)


def _done(
    db: Database, run: dict[str, Any], payload: dict[str, Any],
    draft: ReportDraft | None, step_id: str,
) -> dict[str, Any]:
    if draft is None:
        return _fail(db, run, payload, "No draft found", step_id)
    raw = {
        "ok": True,
        "text": draft.text,
        "title": draft.title,
        "charts": draft.charts,
        "chart_status": draft.chart_status,
        "named_period": draft.named_period,
        "named_period_empty": draft.named_period_empty,
        "web_notes": draft.web_notes,
        "warnings": draft.warnings,
        "report_pending_id": f"rp_{uuid.uuid4().hex[:12]}",
        "data_query": draft.data_query,
    }
    result = {
        "id": payload.get("task_id"),
        "kind": payload.get("kind"),
        "name": "report.draft",
        "ok": True,
        "error": None,
        "payload": raw,
    }
    if step_id:
        _close_report_step(db, run, step_id, payload, raw, None, checkpoint_state([], [], {}, extra={
            REPORT_KEY: {"phase": "done", "step_id": step_id, "result": result},
        }))
    finish_child(db, run, result)
    return result


def _fail(
    db: Database, run: dict[str, Any], payload: dict[str, Any], error: str, step_id: str,
) -> dict[str, Any]:
    if step_id:
        _close_report_step(db, run, step_id, payload, None, {"message": error})
    result = {
        "id": payload.get("task_id"),
        "kind": payload.get("kind"),
        "name": "report.draft",
        "ok": False,
        "error": error,
        "payload": {},
    }
    finish_child(db, run, result)
    return result


def _close_decision(
    db: Database, run_id: str, purpose: str, decision: dict[str, Any], tasks: list[PlanTask],
) -> None:
    """Close a decision step and store its checkpoint in that same transaction."""
    step = db.insert_step(run_id, StepType.MODEL, step_name(StepType.MODEL, None), {"purpose": purpose})
    if step is None:
        return
    db.finish_step(
        step["id"], StepStatus.SUCCEEDED, decision,
        None, checkpoint_state(tasks, [], {}, extra={REPORT_KEY: decision}),
    )


def _close_report_step(
    db: Database, run: dict[str, Any], step_id: str,
    payload: dict[str, Any], output: dict[str, Any] | None, error: dict[str, Any] | None,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    ok = error is None
    saved = db.insert_tool_call(
        run["id"], step_id,
        tool_name="report.draft", args=payload.get("input") if isinstance(payload.get("input"), dict) else {},
        result=output or {},
        status=ToolCallStatus.SUCCEEDED if ok else ToolCallStatus.FAILED,
        idempotency_key=f"report.draft:{run['id']}:{payload.get('task_id') or step_id}",
        latency_ms=0, error=error,
        step_status=StepStatus.SUCCEEDED if ok else StepStatus.FAILED,
        step_output=output, step_error=error,
        checkpoint=checkpoint,
    )
    if saved is None:
        db.finish_step(
            step_id,
            StepStatus.SUCCEEDED if ok else StepStatus.FAILED,
            output, error, checkpoint,
        )


def _open_step(db: Database, run: dict[str, Any], payload: dict[str, Any]) -> str:
    step = db.insert_step(
        run["id"], StepType.TOOL, step_name(StepType.TOOL, "report.draft"),
        payload.get("input") if isinstance(payload.get("input"), dict) else {},
    )
    return "" if step is None else str(step["id"])


def _first_wave(
    questions: list[str], searches: list[str],
) -> tuple[list[PlanTask], list[list[str]], list[list[str]]]:
    """One wave, `depends_on=[]`. Workers claim from the front of this list.

    Zip data with search so a short `web_search` is not stuck behind every `q*`
    when seats are fewer than the wave. IDs stay `q1`/`s1`; only enqueue order
    changes. Pair lists stay in q-then-s identity order for resume.
    """
    data = _data_tasks(questions)
    lookup = _search_tasks(searches)
    question_pairs = [[task.id, str(task.input.get("question") or "")] for task in data]
    search_pairs = [[task.id, str(task.input.get("query") or "")] for task in lookup]
    return _interleave(data, lookup), question_pairs, search_pairs


def _interleave(data: list[PlanTask], lookup: list[PlanTask]) -> list[PlanTask]:
    tasks: list[PlanTask] = []
    for index in range(max(len(data), len(lookup))):
        if index < len(data):
            tasks.append(data[index])
        if index < len(lookup):
            tasks.append(lookup[index])
    return tasks


def _data_tasks(questions: list[str], *, start: int = 1) -> list[PlanTask]:
    tasks: list[PlanTask] = []
    for index, question in enumerate(questions, start=start):
        tasks.append(PlanTask(
            id=f"q{index}", kind="agent", name="data",
            input={"question": question, "deliver": "data", "columns": []}, depends_on=[],
        ))
    return tasks


def _search_tasks(queries: list[str]) -> list[PlanTask]:
    tasks: list[PlanTask] = []
    for index, query in enumerate(queries, start=1):
        tasks.append(PlanTask(
            id=f"s{index}", kind="tool", name="web_search",
            input={"query": query}, depends_on=[],
        ))
    return tasks


def _take_draft(items: Any) -> ReportDraft | None:
    draft: ReportDraft | None = None
    for item in items:
        if isinstance(item, ReportDraft):
            draft = item
    return draft


def _remembered(db: Database, session: dict[str, Any]) -> str:
    return prompt_context(
        standing_block(db, session["partnership_id"], actor_id(db, session["id"])),
        summary_block(session.get("summary")),
    )


def _trace(db: Database, run_id: str, step_id: str):
    from ..report.turn import _RunTrace

    return _RunTrace(db=db, run_id=run_id, step_id=step_id or None)
