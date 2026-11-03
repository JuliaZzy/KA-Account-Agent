"""One harness pass over a run the worker has already claimed.

The checkpoint is the memory. `last` says which side of a tool call that memory
stopped on. A model iteration and a tool iteration each become one `agent_steps`
row after that side has been stored. `open_step_id` is set only while a tool
step is waiting for the user; it is not a step name.

The worker claims a lease, then calls `execute_run`. Creating through the
outbox and claiming that same job in this process is `run_in_this_process`.
Both enter `run_loop`.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import json
import logging
import smtplib
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from ..db import Database, utcnow
from ..llm.types import AssistantTurn
from ..repositories.run_repository import (
    RunStatus,
    append_checkpoint,
    append_run_event,
    claim_tool_call,
    complete_tool_call,
    count_tool_calls,
    create_agent_run,
    find_agent_run_by_id,
    finish_step,
    insert_model_call,
    insert_step,
    insert_tool_call,
    latest_checkpoint,
    list_run_events,
    mark_waiting_for_user,
)
from ..tools.send_email import MailError
from ..tools.send_email import preflight as mail_preflight
from .entry_guard import acquire_tool, release_tool, tool_group, tool_switch_off
from .run_names import (
    COMPLETED_TOOL_CALLS_KEY,
    CONTEXT_KEY,
    LAST_INPUT,
    LAST_KEY,
    LAST_REPLY,
    EntryErrorCode,
    StepEvent,
    StepStatus,
    StepType,
    ToolCallStatus,
    ToolErrorCode,
    checkpoint_last_after_tool,
    checkpoint_last_before_tool,
    step_name,
    tool_error,
)
from .run_worker import LeaseLost, PermanentRunError

MAX_LOOP_STEPS = 8
# A read tool may be called again this many times after the first failure.
# Side effects are not retried.
_READ_RETRIES = 2
_TOOL_USE = "assistant:tool_use:"
Memory = tuple[list[dict[str, Any]], list[str], str, str | None, str | None]
log = logging.getLogger(__name__)

@dataclass(frozen=True)
class Tool:
    """One registered tool. `schema` is what the model sees.

    `execute_tool` reads `side_effect` when it writes the audit row,
    `idempotency_required` when it builds the business key, and `timeout_seconds`
    when it stops a slow call. `retryable_errors` decides whether a read tool
    is tried again. `display` is the sentence already stored on events.
    """

    name: str
    description: str
    fn: Callable[[dict[str, Any]], dict[str, Any]]
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    requires_approval: bool = False
    side_effect: bool = False
    idempotency_required: bool = False
    timeout_seconds: int = 30
    retryable_errors: frozenset[str] = field(default_factory=frozenset)
    display: str = ""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class _Prepared:
    kind: str
    context: list[dict[str, Any]]
    completed: list[str]
    tool: Tool | None = None
    row_id: str | None = None
    step_id: str | None = None
    tool_use_id: str | None = None
    args: dict[str, Any] = field(default_factory=dict)

class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            step_name(StepType.TOOL, tool.name)
            if tool.name in self._tools:
                raise ValueError(f"Tool {tool.name} already registered")
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self.find(name)
        if tool is None:
            raise ValueError(f"Tool {name} not found")
        return tool

    def find(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

def _tool_failure(tool: Tool, message: str) -> dict[str, str | bool]:
    """A handler that did not return a result. Retry only if this tool lists that code."""
    code = ToolErrorCode.UPSTREAM
    return tool_error(code, message, retryable=code in tool.retryable_errors)


def _caller(fn: Any) -> tuple[str, str]:
    return str(getattr(fn, "provider", "fake")), str(getattr(fn, "model", "fake"))

def execute_run(
    db: Database,
    tools: ToolRegistry,
    *,
    decide: Callable[[list[dict[str, Any]], list[dict[str, Any]]], AssistantTurn],
    answer: Callable[[list[dict[str, Any]]], str],
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The callable `work_once` takes. The database and the model stay in the closure."""
    def execute(run: dict[str, Any]) -> dict[str, Any]:
        return run_loop(db, run, tools, decide, answer)
    return execute

def run_in_this_process(
    db: Database,
    org_id: str,
    user_id: str,
    run_type: str,
    idempotency_key: str,
    run_input: dict[str, Any],
    tools: ToolRegistry,
    decide: Callable[[list[dict[str, Any]], list[dict[str, Any]]], AssistantTurn],
    answer: Callable[[list[dict[str, Any]]], str],
    *,
    parent_run_id: str | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Create through the outbox, then claim this job and start the run here."""
    from .dispatcher import dispatch_pending
    from .run_worker import work_once

    owner = worker_id or user_id
    run, _created = create_agent_run(db, org_id, user_id, run_type, idempotency_key, run_input, parent_run_id)
    dispatch_pending(db)
    work_once(db, owner, execute_run(db, tools, decide=decide, answer=answer), run_id=run["id"])
    row = find_agent_run_by_id(db, run["id"])
    if row is None:
        raise RuntimeError("run disappeared before it started")
    return row

def run_loop(
    db: Database, 
    run: dict[str, Any], 
    tools: ToolRegistry, 
    decide: Callable[[list[dict[str, Any]], list[dict[str, Any]]], AssistantTurn], 
    answer: Callable[[list[dict[str, Any]]], str]
) -> dict[str, Any]:
    owner = run.get("lease_owner")
    if not isinstance(owner, str) or not owner:
        raise LeaseLost("claimed run has no lease owner")
    decide_provider, decide_model_name = _caller(decide)
    answer_provider, answer_model_name = _caller(answer)
    loaded = _load(db, run)
    if loaded is None or _stopped(db, run["id"], owner):
        return{}
    context, completed, last, open_step_id, resume_note = loaded
    if last == LAST_REPLY:
        return {"text": _assistant_text(context)}
    for _ in range(MAX_LOOP_STEPS):
        if _stopped(db, run["id"], owner):
            return {}
        if last.startswith(_TOOL_USE):
            moved = _finish_pending_tools(
                db, run, tools, context, completed, open_step_id, resume_note,
                None, 0, decide_provider, decide_model_name,
            )
            if moved is None:
                return {}
            context, completed, last, open_step_id, resume_note = moved
            continue
        started = time.perf_counter()
        _event(db, run, StepEvent.MODEL_STARTED, {})
        turn = _decide(decide, context, tools)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if turn.tool_calls:
            call = turn.tool_calls[0]
            context = [*context, _assistant_message(turn)]
            if _save(db, run["id"], context, completed, checkpoint_last_before_tool(call.name)) is None:
                return {}
            moved = _finish_pending_tools(
                db, run, tools, context, completed, None, resume_note, turn, latency_ms,
                decide_provider, decide_model_name,
            )
            if moved is None:
                return {}
            context, completed, last, open_step_id, resume_note = moved
            continue 
        text = (turn.content or "").strip()
        reply_provider, reply_model_name, reply_ms = decide_provider, decide_model_name, latency_ms
        if not text:
            started = time.perf_counter()
            text = answer(context).strip()
            reply_ms = int((time.perf_counter() - started) * 1000)
            reply_provider, reply_model_name = answer_provider, answer_model_name
            turn = AssistantTurn(
                content=text,
                prompt_tokens=int(getattr(answer, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(answer, "completion_tokens", 0) or 0),
            )
        if not text:
            raise PermanentRunError("model returned empty content")
        context = [*context, _assistant_message(turn)]
        if _save(db, run["id"], context, completed, LAST_REPLY) is None:
            return {}
        if _record_model_step(db, run, context, turn, reply_ms, reply_provider, reply_model_name, {"text": text}) is None:
            return {}
        return {"text": text}
    raise PermanentRunError("max loop steps exceeded")

def _load(db: Database, run: dict[str, Any]) -> Memory | None:
    row = latest_checkpoint(db, run["id"])
    if row is None:
        context = [{"role": "user", "content": _user_text(run.get("input_json") or {})}]
        if _save(db, run["id"], context, [], LAST_INPUT, last_report=_input_report(run)) is None:
            return None 
        return context, [], LAST_INPUT, None, None
    state = row["state_json"]
    context = list(state[CONTEXT_KEY])
    completed = [item for item in state[COMPLETED_TOOL_CALLS_KEY] if isinstance(item, str)]
    last = state[LAST_KEY]
    open_step_id = state.get("open_step_id") if isinstance(state.get("open_step_id"), str) else None
    note = _resume_note(db, run, row["created_at"])
    if note and last.startswith(_TOOL_USE):
        return context, completed, last, open_step_id, note
    if note and last != LAST_REPLY:
        context = [*context, {"role": "user", "content": note}]
        if _save(db, run["id"], context, completed, LAST_INPUT) is None:
            return None
        return context, completed, LAST_INPUT, None, None
    return context, completed, last, open_step_id, None

def _invoke_tool(
    db: Database,
    run_id: str,
    tool: Tool,
    args: dict[str, Any],
    context: list[dict[str, Any]],
    step_id: str | None = None,
) -> Any:
    payload = dict(args)
    if _accepts_context(tool.fn):
        return tool.fn(
            payload, 
            context=_tool_context(context, _checkpoint_report(db, run_id), run_id=run_id, step_id=step_id)
        )
    return tool.fn(payload)

def _accepts_context(fn: Callable[[dict[str, Any], dict[str, Any]], Any]) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "context" in params:
        return True
    return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values())

def _tool_context(
    context: list[dict[str, Any]], 
    last_report: str = "",
    *, 
    run_id: str | None = None,
    step_id: str | None = None,
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    for message in context:
        if not isinstance(message, dict) or message.get("role") not in ("assistant", "user"):
            continue 
        text = _plain_text(message.get("content"))
        if text:
            history.append({"role": str(message["role"]), "content": text})
    payload: dict[str, Any] = {"history": history, "run_id": run_id}
    if step_id:
        payload["step_id"] = step_id
    if last_report:
        payload["last_report"] = last_report
    return payload

def _plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip()
    ]
    return "\n".join(parts)

def _call_from_block(block: dict[str, Any]) -> Any:
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    return SimpleNamespace(id=block.get("id"), name=block.get("name"), arguments=args)


def _needs_approval(tools: ToolRegistry, block: dict[str, Any]) -> bool:
    tool = tools.find(str(block.get("name") or ""))
    return tool is not None and tool.requires_approval


def _assistant_tool_uses(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content = _last_assistant(context).get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]


def _pending_tool_uses(context: list[dict[str, Any]], completed: list[str]) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    for block in _assistant_tool_uses(context):
        tool_use_id = block.get("id")
        if isinstance(tool_use_id, str) and tool_use_id not in completed:
            pending.append(block)
    return pending


def _finish_pending_tools(
    db: Database,
    run: dict[str, Any],
    tools: ToolRegistry,
    context: list[dict[str, Any]],
    completed: list[str],
    open_step_id: str | None,
    resume_note: str | None,
    turn: AssistantTurn | None,
    latency_ms: int,
    provider: str,
    model_name: str,
) -> Memory | None:
    """Finish every uncompleted tool_use on the last assistant turn.

    Independent calls in that turn run in parallel. Approval tools wait: a fresh
    decide pauses them; a resume runs or skips them. The next model call only
    happens after every tool_use has a result.
    """
    pending = _pending_tool_uses(context, completed)
    if not pending:
        raise PermanentRunError("checkpoint tool call does not match the context")
    last = checkpoint_last_after_tool(str(pending[-1].get("name") or "tool"))
    ready = [block for block in pending if not _needs_approval(tools, block)]
    waiting = [block for block in pending if _needs_approval(tools, block)]
    if waiting and _resume_skipped(db, run):
        for block in waiting:
            moved = _finish_tool(
                db, run, tools, context, completed, str(block.get("name") or ""),
                open_step_id, resume_note, turn, latency_ms, provider, model_name,
            )
            if moved is None:
                return None
            context, completed, last, open_step_id, resume_note = moved
            turn, latency_ms, open_step_id = None, 0, None
        waiting = []
    if ready:
        moved = _run_tool_batch(
            db, run, tools, context, completed, ready, open_step_id, resume_note,
            turn, latency_ms, provider, model_name, parallel=True,
        )
        if moved is None:
            return None
        context, completed, last, open_step_id, resume_note = moved
        turn, latency_ms, open_step_id = None, 0, None
    if waiting:
        if turn is not None:
            call = _call_from_block(waiting[0])
            if _pause(db, run, context, completed, call, turn, latency_ms, provider, model_name) is None:
                return None
            return None
        moved = _run_tool_batch(
            db, run, tools, context, completed, waiting, open_step_id, resume_note,
            None, 0, provider, model_name, parallel=False,
        )
        if moved is None:
            return None
        context, completed, last, open_step_id, resume_note = moved
    return context, completed, last, open_step_id, resume_note


def _run_tool_batch(
    db: Database,
    run: dict[str, Any],
    tools: ToolRegistry,
    context: list[dict[str, Any]],
    completed: list[str],
    blocks: list[dict[str, Any]],
    open_step_id: str | None,
    resume_note: str | None,
    turn: AssistantTurn | None,
    latency_ms: int,
    provider: str,
    model_name: str,
    *,
    parallel: bool,
) -> Memory | None:
    if len(blocks) == 1:
        return _finish_tool(
            db, run, tools, context, completed, str(blocks[0].get("name") or ""),
            open_step_id, resume_note, turn, latency_ms, provider, model_name,
        )
    if not parallel:
        moved: Memory | None = None
        for index, block in enumerate(blocks):
            moved = _finish_tool(
                db, run, tools, context, completed, str(block.get("name") or ""),
                open_step_id if index == 0 else None,
                resume_note, turn if index == 0 else None,
                latency_ms if index == 0 else 0, provider, model_name,
            )
            if moved is None:
                return None
            context, completed, last, open_step_id, resume_note = moved
            turn = None
        return moved
    return _run_tools_parallel(
        db, run, tools, context, completed, blocks, open_step_id, resume_note,
        turn, latency_ms, provider, model_name,
    )


def _run_tools_parallel(
    db: Database,
    run: dict[str, Any],
    tools: ToolRegistry,
    context: list[dict[str, Any]],
    completed: list[str],
    blocks: list[dict[str, Any]],
    open_step_id: str | None,
    resume_note: str | None,
    turn: AssistantTurn | None,
    latency_ms: int,
    provider: str,
    model_name: str,
) -> Memory | None:
    jobs: list[tuple[dict[str, Any], str]] = []
    for index, block in enumerate(blocks):
        tool_name = str(block.get("name") or "")
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        try:
            step_name(StepType.TOOL, tool_name)
        except ValueError as exc:
            raise PermanentRunError(str(exc)) from exc
        if index == 0 and open_step_id:
            step_id = open_step_id
        elif index == 0:
            step_id = _record_model_step(
                db, run, context, turn, latency_ms, provider, model_name, None, tool_name, args,
            )
        else:
            step = insert_step(db, run["id"], StepType.TOOL, tool_name, args)
            step_id = None if step is None else step["id"]
        if step_id is None:
            return None
        jobs.append((block, step_id))
    prepared: list[_Prepared] = []
    for block, step_id in jobs:
        tool_name = str(block.get("name") or "")
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        tool_use_id = str(block.get("id") or "")
        prep = _prepare_tool_call(
            db, run, tools, step_id=step_id, tool_name=tool_name, args=args,
            tool_use_id=tool_use_id, context=context, completed=completed,
        )
        if prep is None:
            return None
        if prep.kind != "claimed":
            context, completed = prep.context, prep.completed
            continue
        prepared.append(prep)
    if prepared:
        org_id = str(run.get("org_id") or "")

        def _run(prep: _Prepared) -> _Attempt:
            assert prep.tool is not None and prep.step_id is not None
            return _attempt_tool(
                db, run["id"], prep.tool, prep.args, context, prep.step_id, org_id,
            )

        attempts: dict[str, tuple[_Prepared, _Attempt]] = {}
        if len(prepared) == 1:
            prep = prepared[0]
            attempts[str(prep.tool_use_id)] = (prep, _run(prep))
        else:
            # One Context cannot be entered by two threads. Copy per task.
            with ThreadPoolExecutor(max_workers=min(5, len(prepared))) as pool:
                futs = {pool.submit(copy_context().run, _run, prep): prep for prep in prepared}
                for fut in as_completed(futs):
                    prep = futs[fut]
                    try:
                        attempt = fut.result()
                    except Exception as exc:
                        attempt = _Attempt(
                            {}, ToolCallStatus.FAILED, _tool_failure(prep.tool, str(exc)),
                            False, 0, False,
                        )
                    attempts[str(prep.tool_use_id)] = (prep, attempt)
        for block in blocks:
            item = attempts.get(str(block.get("id") or ""))
            if item is None:
                continue
            prep, attempt = item
            assert prep.tool is not None and prep.row_id is not None and prep.tool_use_id is not None
            moved = _close_tool(
                db, run, prep.tool, prep.row_id, prep.tool_use_id, context, completed,
                result=attempt.result, status=attempt.status, latency_ms=attempt.latency_ms,
                error=attempt.error, executed=attempt.executed, manual_review=attempt.manual_review,
            )
            if moved is None:
                return None
            context, completed = moved
    return _after_tool_note(db, run, context, completed, str(blocks[-1].get("name") or ""), resume_note)


def _finish_tool(
    db: Database,
    run: dict[str, Any],
    tools: ToolRegistry,
    context: list[dict[str, Any]],
    completed: list[str],
    tool_name: str,
    open_step_id: str | None,
    resume_note: str | None,
    turn: AssistantTurn | None,
    latency_ms: int,
    provider: str,
    model_name: str,
) -> Memory | None:
    """Loop side of one tool. Skip stays here. Everything else goes through `execute_tool`."""
    block = _last_tool_use(context, tool_name)
    if block is None or block.get("name") != tool_name:
        raise PermanentRunError("checkpoint tool call does not match the context")
    tool_use_id = block.get("id")
    if not isinstance(tool_use_id, str) or tool_use_id in completed:
        raise PermanentRunError("tool call id is missing or already completed")
    tool = tools.find(tool_name)
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    if _stopped(db, run["id"], str(run.get("lease_owner") or "")):
        return None

    step_id = open_step_id
    # Skip never calls the handler and never takes a seat.
    if tool is not None and tool.requires_approval and _resume_skipped(db, run):
        if step_id is None:
            return None
        result = {"skipped": True}
        context = [*context, _tool_result_message(tool_use_id, result)]
        completed = [*completed, tool_use_id]
        saved = insert_tool_call(
            db, run["id"], step_id,
            tool_name=tool_name, args=args, result=result, status=ToolCallStatus.SUCCEEDED,
            idempotency_key=f"{tool_name}:{tool_use_id}", latency_ms=0,
            checkpoint=_checkpoint_state(
                db, run["id"], context, completed, checkpoint_last_after_tool(tool_name),
            ),
            step_status=StepStatus.SUCCEEDED, step_output=result,
        )
        if saved is None:
            return None
        return _after_tool_note(db, run, context, completed, tool_name, resume_note)

    if step_id is None:
        try:
            step_name(StepType.TOOL, tool_name)
        except ValueError as exc:
            raise PermanentRunError(str(exc)) from exc
        step_id = _record_model_step(
            db, run, context, turn, latency_ms, provider, model_name, None, tool_name, args,
        )
    if step_id is None:
        return None
    moved = execute_tool(
        db, run, tools, step_id=step_id, tool_name=tool_name, args=args,
        tool_use_id=tool_use_id, context=context, completed=completed,
    )
    if moved is None:
        return None
    context, completed = moved
    return _after_tool_note(db, run, context, completed, tool_name, resume_note)


def execute_tool(
    db: Database,
    run: dict[str, Any],
    tools: ToolRegistry,
    *,
    step_id: str,
    tool_name: str,
    args: dict[str, Any],
    tool_use_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Claim a model tool, call it outside the database transaction, then write the step back.

    The handler is not called when the tool is missing, the arguments do not match
    `parameters`, the run is over `max_tool_calls`, the user is outside this
    distributor, a switch is holding the tool, or this idempotency key already
    has a row. A validation failure is stored under `{tool_name}:{tool_use_id}` and
    does not take the seat used for a real call. `max_tool_calls` is read from the
    run. A missing value means no cap. Over the cap, the run fails and the handler
    is not called.
    A handler error is stored and returned to the loop. It does not fail the run.
    None means the lease is gone or the step could not be written.
    """
    prepared = _prepare_tool_call(
        db, run, tools, step_id=step_id, tool_name=tool_name, args=args,
        tool_use_id=tool_use_id, context=context, completed=completed,
    )
    if prepared is None:
        return None
    if prepared.kind != "claimed":
        return prepared.context, prepared.completed
    assert prepared.tool is not None and prepared.row_id is not None and prepared.step_id is not None
    attempt = _attempt_tool(
        db, run["id"], prepared.tool, prepared.args, prepared.context, prepared.step_id,
        str(run.get("org_id") or ""),
    )
    return _close_tool(
        db, run, prepared.tool, prepared.row_id, tool_use_id, prepared.context, prepared.completed,
        result=attempt.result, status=attempt.status, latency_ms=attempt.latency_ms,
        error=attempt.error, executed=attempt.executed, manual_review=attempt.manual_review,
    )


def _prepare_tool_call(
    db: Database,
    run: dict[str, Any],
    tools: ToolRegistry,
    *,
    step_id: str,
    tool_name: str,
    args: dict[str, Any],
    tool_use_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
) -> _Prepared | None:
    """Claim the seat or record a rejection. The handler is not called here."""
    if _stopped(db, run["id"], str(run.get("lease_owner") or "")):
        return None
    tool = tools.find(tool_name)
    if tool is None:
        error = tool_error(ToolErrorCode.NOT_FOUND, "tool is not registered")
        moved = _reject_tool(
            db, run, step_id, tool_name, tool_use_id, context, completed, error, executed=False,
        )
        return None if moved is None else _Prepared("done", moved[0], moved[1])
    paused = tool_switch_off(
        tool.name, side_effect=tool.side_effect, idempotency_required=tool.idempotency_required,
    )
    if paused is not None:
        mark_waiting_for_user(db, run["id"], {
            "tool_name": tool.name, "reason": "tool_paused", "switch": paused,
        })
        return None
    problem = _argument_error(tool.parameters, args)
    if problem is not None:
        error = tool_error(
            ToolErrorCode.VALIDATION, problem,
            retryable=ToolErrorCode.VALIDATION in tool.retryable_errors,
        )
        moved = _record_invalid_call(
            db, run, step_id, tool_name, args, tool_use_id, context, completed, error,
        )
        return None if moved is None else _Prepared("done", moved[0], moved[1])
    if tool.name == "send_email":
        refusal = mail_preflight(args)
        if refusal is not None:
            code, message = refusal
            error = tool_error(code, message, retryable=code in tool.retryable_errors)
            moved = _record_invalid_call(
                db, run, step_id, tool_name, args, tool_use_id, context, completed, error,
            )
            return None if moved is None else _Prepared("done", moved[0], moved[1])
    if tool.name == "send_email" or (tool.side_effect and tool.idempotency_required):
        person = db.user(str(run.get("user_id") or ""))
        if person is None or person.distributor_id != run.get("org_id"):
            error = tool_error(ToolErrorCode.PERMISSION_DENIED, "user is not in this distributor")
            _reject_tool(
                db, run, step_id, tool_name, tool_use_id, context, completed, error, executed=False,
            )
            raise PermanentRunError(ToolErrorCode.PERMISSION_DENIED)
    cap = run.get("max_tool_calls")
    if cap is not None and count_tool_calls(db, run["id"]) >= int(cap):
        error = tool_error(EntryErrorCode.COST_BUDGET_EXCEEDED, "tool call budget exceeded")
        _reject_tool(
            db, run, step_id, tool_name, tool_use_id, context, completed, error, executed=False,
        )
        raise PermanentRunError(EntryErrorCode.COST_BUDGET_EXCEEDED)

    key = _idempotency_key(tool, run, args, tool_use_id)
    if key is None:
        error = tool_error(
            ToolErrorCode.IDEMPOTENCY_REQUIRED, "idempotency key could not be computed",
            retryable=ToolErrorCode.IDEMPOTENCY_REQUIRED in tool.retryable_errors,
        )
        moved = _record_invalid_call(
            db, run, step_id, tool_name, args, tool_use_id, context, completed, error,
        )
        return None if moved is None else _Prepared("done", moved[0], moved[1])
    claimed = claim_tool_call(
        db, run["id"], step_id, tool_name=tool_name, args=args, idempotency_key=key,
    )
    if claimed is None:
        return None
    row, created = claimed
    if not created:
        moved = _reuse_tool_call(
            db, run, tool, row, step_id, tool_use_id, context, completed,
        )
        return None if moved is None else _Prepared("done", moved[0], moved[1])

    _event(db, run, StepEvent.TOOL_STARTED, {"tool_name": tool_name, "tool_use_id": tool_use_id})
    if _stopped(db, run["id"], str(run.get("lease_owner") or "")):
        return None
    return _Prepared(
        "claimed", context, completed, tool=tool, row_id=row["id"],
        step_id=step_id, tool_use_id=tool_use_id, args=args,
    )


def _idempotency_key(
    tool: Tool, run: dict[str, Any], args: dict[str, Any], tool_use_id: str,
) -> str | None:
    """Seat key. Read tools use this call's id. Side effects use a business key.

    The business key contains `org_id` and not `run_id` or `tool_use_id`, so the
    same email or MCP call on another run finds the existing row. None means the
    key cannot be built; the caller must not claim a seat.
    """
    if not tool.idempotency_required:
        return f"{tool.name}:{tool_use_id}"
    org_id = str(run.get("org_id") or "")
    if not org_id:
        return None
    if tool.name == "send_email":
        to_address = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "").strip()
        digest = hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()[:16]
        return f"send_email:{org_id}:{to_address}:{digest}"
    payload = json.dumps(args, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"mcp:{org_id}:{tool.name}:{digest}"


@dataclass(frozen=True)
class _Attempt:
    result: dict[str, Any]
    status: str
    error: dict[str, Any] | None
    executed: bool
    latency_ms: int
    manual_review: bool


def _attempt_tool(
    db: Database,
    run_id: str,
    tool: Tool,
    args: dict[str, Any],
    context: list[dict[str, Any]],
    step_id: str,
    org_id: str,
) -> _Attempt:
    """Call the handler until it succeeds, the error is final, or retries run out.

    A read tool is called again at most `_READ_RETRIES` times when the error code
    is listed on the tool. One seat covers every try. A side effect is never
    called again; a timeout becomes `response_lost` and asks for a person.
    """
    retries_left = 0 if tool.side_effect else _READ_RETRIES
    latency_ms = 0
    while True:
        one = _one_call(db, run_id, tool, args, context, step_id, org_id)
        latency_ms += one.latency_ms
        if one.status == ToolCallStatus.SUCCEEDED:
            return _Attempt(one.result, one.status, None, True, latency_ms, False)
        if one.status == ToolCallStatus.RESPONSE_LOST:
            return _Attempt({}, one.status, one.error, True, latency_ms, True)
        if retries_left > 0 and one.error is not None and one.error.get("retryable"):
            retries_left -= 1
            continue
        return _Attempt({}, ToolCallStatus.FAILED, one.error, one.executed, latency_ms, False)


def _one_call(
    db: Database,
    run_id: str,
    tool: Tool,
    args: dict[str, Any],
    context: list[dict[str, Any]],
    step_id: str,
    org_id: str,
) -> _Attempt:
    started = time.perf_counter()
    group = tool_group(tool.name)
    if group is not None and not acquire_tool(org_id, group):
        error = tool_error(
            ToolErrorCode.RATE_LIMITED, "tool concurrency is full",
            retryable=ToolErrorCode.RATE_LIMITED in tool.retryable_errors,
        )
        return _Attempt({}, ToolCallStatus.FAILED, error, False, 0, False)
    try:
        result = _invoke_limited(
            lambda: _invoke_tool(db, run_id, tool, args, context, step_id),
            tool.timeout_seconds,
        )
    except TimeoutError:
        error = tool_error(
            ToolErrorCode.TIMEOUT, "tool timed out",
            retryable=ToolErrorCode.TIMEOUT in tool.retryable_errors,
        )
        if tool.side_effect:
            return _Attempt({}, ToolCallStatus.RESPONSE_LOST, error, True, _elapsed_ms(started), True)
        return _Attempt({}, ToolCallStatus.FAILED, error, True, _elapsed_ms(started), False)
    except MailError as exc:
        error = tool_error(exc.code, str(exc), retryable=exc.code in tool.retryable_errors)
        return _Attempt({}, ToolCallStatus.FAILED, error, True, _elapsed_ms(started), False)
    except Exception as exc:
        left = _request_never_left(exc)
        return _Attempt(
            {}, ToolCallStatus.FAILED, _tool_failure(tool, str(exc)), not left, _elapsed_ms(started), False,
        )
    finally:
        if group is not None:
            release_tool(org_id, group)
    if not isinstance(result, dict):
        return _Attempt(
            {}, ToolCallStatus.FAILED, _tool_failure(tool, "tool returned a non-object"),
            True, _elapsed_ms(started), False,
        )
    return _Attempt(result, ToolCallStatus.SUCCEEDED, None, True, _elapsed_ms(started), False)


def _invoke_limited(call: Callable[[], Any], timeout_seconds: int) -> Any:
    """Run `call` on a daemon thread and give up when `timeout_seconds` elapses.

    The thread is not stopped. A side effect may still finish after this returns;
    the caller records that as `response_lost` and does not wait for it.
    """
    if timeout_seconds <= 0:
        return call()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = call()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimeoutError("tool timed out")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _reuse_tool_call(
    db: Database,
    run: dict[str, Any],
    tool: Tool,
    row: dict[str, Any],
    step_id: str,
    tool_use_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """A seat already exists. Do not call the handler again."""
    if row.get("step_id") != step_id:
        return _duplicate_side_effect(db, run, tool, row, step_id, tool_use_id, context, completed)
    status = row.get("status")
    if status == ToolCallStatus.RUNNING:
        error = tool_error(ToolErrorCode.UPSTREAM, "tool call was left running")
        return _close_tool(
            db, run, tool, row["id"], tool_use_id, context, completed,
            result={}, status=ToolCallStatus.RESPONSE_LOST, latency_ms=int(row.get("latency_ms") or 0),
            error=error, executed=False, manual_review=bool(tool.side_effect),
        )
    if status == ToolCallStatus.SUCCEEDED:
        stored = row.get("result_json")
        result = stored if isinstance(stored, dict) else {}
        return _close_tool(
            db, run, tool, row["id"], tool_use_id, context, completed,
            result=result, status=ToolCallStatus.SUCCEEDED, latency_ms=int(row.get("latency_ms") or 0),
            error=None, executed=False,
        )
    stored_error = row.get("error_json")
    error = stored_error if isinstance(stored_error, dict) and stored_error else tool_error(
        ToolErrorCode.UPSTREAM, "tool call already finished",
    )
    return _close_tool(
        db, run, tool, row["id"], tool_use_id, context, completed,
        result={}, status=ToolCallStatus.FAILED, latency_ms=int(row.get("latency_ms") or 0),
        error=error, executed=False,
    )


def _duplicate_side_effect(
    db: Database,
    run: dict[str, Any],
    tool: Tool,
    row: dict[str, Any],
    step_id: str,
    tool_use_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """The business key belongs to another step. Do not insert a second row."""
    status = row.get("status")
    if status == ToolCallStatus.RUNNING:
        error = tool_error(ToolErrorCode.UPSTREAM, "tool call was left running")
        saved = complete_tool_call(
            db, row["id"], result={}, status=ToolCallStatus.RESPONSE_LOST,
            latency_ms=int(row.get("latency_ms") or 0), error=error,
            audit=bool(tool.side_effect), manual_review=bool(tool.side_effect),
        )
        if saved is None:
            return None
        return _reject_tool(
            db, run, step_id, tool.name, tool_use_id, context, completed, error, executed=False,
        )
    if status == ToolCallStatus.SUCCEEDED:
        stored = row.get("result_json")
        visible = {"skipped_duplicate": True, "result": stored if isinstance(stored, dict) else {}}
        failed = False
    else:
        stored_error = row.get("error_json")
        error = stored_error if isinstance(stored_error, dict) and stored_error else tool_error(
            ToolErrorCode.UPSTREAM, "tool call already finished",
        )
        visible = {"skipped_duplicate": True, "error": error}
        failed = True
    context = [*context, _tool_result_message(tool_use_id, visible)]
    completed = [*completed, tool_use_id]
    step_status = StepStatus.FAILED if failed else StepStatus.SUCCEEDED
    event = StepEvent.TOOL_FAILED if failed else StepEvent.TOOL_COMPLETED
    saved = finish_step(
        db, step_id, step_status, visible, visible if failed else None,
        checkpoint=_checkpoint_state(
            db, run["id"], context, completed, checkpoint_last_after_tool(tool.name),
        ),
        extra_event=event,
        extra_payload={
            "step_id": step_id,
            "tool_name": tool.name,
            "status": "failed" if failed else "succeeded",
            "skipped_duplicate": True,
        },
    )
    if saved is None:
        return None
    return context, completed


def _close_tool(
    db: Database,
    run: dict[str, Any],
    tool: Tool,
    tool_call_id: str,
    tool_use_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
    *,
    result: dict[str, Any],
    status: str,
    latency_ms: int,
    error: dict[str, Any] | None,
    executed: bool,
    manual_review: bool = False,
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Write the tool result, the step, the checkpoint, and a side-effect audit row together."""
    visible = result if status == ToolCallStatus.SUCCEEDED else (error or {})
    failed = status != ToolCallStatus.SUCCEEDED
    context = [*context, _tool_result_message(tool_use_id, visible if isinstance(visible, dict) else {})]
    completed = [*completed, tool_use_id]
    step_status = StepStatus.FAILED if failed else StepStatus.SUCCEEDED
    saved = complete_tool_call(
        db, tool_call_id, result=result, status=status, latency_ms=latency_ms, error=error,
        checkpoint=_checkpoint_state(
            db, run["id"], context, completed, checkpoint_last_after_tool(tool.name),
        ),
        audit=bool(tool.side_effect) and (executed or status == ToolCallStatus.RESPONSE_LOST),
        step_status=step_status,
        step_output=visible if isinstance(visible, dict) else {},
        step_error=error if failed else None,
        manual_review=manual_review,
    )
    if saved is None:
        return None
    if failed and (executed or status == ToolCallStatus.RESPONSE_LOST):
        _log_tool_failure(
            run, step_id=str(saved.get("step_id") or ""), tool_call_id=tool_call_id,
            tool_name=tool.name, error_code=str((error or {}).get("code") or ""),
            executed=executed, retryable=bool((error or {}).get("retryable")), latency_ms=latency_ms,
        )
    return context, completed


def _record_invalid_call(
    db: Database,
    run: dict[str, Any],
    step_id: str,
    tool_name: str,
    args: dict[str, Any],
    tool_use_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
    error: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Store a validation failure without claiming the call."""
    context = [*context, _tool_result_message(tool_use_id, error)]
    completed = [*completed, tool_use_id]
    saved = insert_tool_call(
        db, run["id"], step_id,
        tool_name=tool_name, args=args, result={}, status=ToolCallStatus.FAILED,
        idempotency_key=f"{tool_name}:{tool_use_id}", latency_ms=0, error=error,
        checkpoint=_checkpoint_state(
            db, run["id"], context, completed, checkpoint_last_after_tool(tool_name),
        ),
        step_status=StepStatus.FAILED, step_output=error, step_error=error,
    )
    if saved is None:
        return None
    _log_tool_failure(
        run, step_id=step_id, tool_call_id=str(saved.get("id") or ""), tool_name=tool_name,
        error_code=str(error.get("code") or ""), executed=False,
        retryable=bool(error.get("retryable")), latency_ms=0,
    )
    return context, completed


def _argument_error(parameters: dict[str, Any], args: dict[str, Any]) -> str | None:
    """First schema mismatch, or None when `args` fits `parameters`.

    Checks `required`, property `type` and `enum`, nested objects, array `items`,
    and `additionalProperties: false`. Other JSON Schema keywords are left alone.
    """
    if not isinstance(args, dict):
        return "arguments must be an object"
    required = parameters.get("required")
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in args:
                return f"missing required property: {name}"
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    for name, value in args.items():
        spec = properties.get(name)
        if not isinstance(spec, dict):
            continue
        message = _property_error(name, spec, value)
        if message is not None:
            return message
    if parameters.get("additionalProperties") is False:
        extra = next((name for name in args if name not in properties), None)
        if extra is not None:
            return f"unexpected property: {extra}"
    return None


def _property_error(name: str, spec: dict[str, Any], value: Any) -> str | None:
    allowed = spec.get("type")
    if isinstance(allowed, str):
        allowed = [allowed]
    if isinstance(allowed, list) and allowed:
        if not any(_matches_type(kind, value) for kind in allowed if isinstance(kind, str)):
            return f"property {name} has the wrong type"
    enum = spec.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"property {name} is not an allowed value"
    if isinstance(value, dict) and spec.get("type") == "object":
        nested = _argument_error(spec, value)
        if nested is not None:
            return f"property {name}: {nested}"
    items = spec.get("items")
    if isinstance(value, list) and isinstance(items, dict):
        for item in value:
            message = _property_error(f"{name}[]", items, item)
            if message is not None:
                return message
    return None


def _matches_type(kind: str, value: Any) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "null":
        return value is None
    return True


def _reject_tool(
    db: Database,
    run: dict[str, Any],
    step_id: str,
    tool_name: str,
    tool_use_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
    error: dict[str, Any],
    *,
    executed: bool,
) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Close the step without a tool row. Used when the handler must not run."""
    context = [*context, _tool_result_message(tool_use_id, error)]
    completed = [*completed, tool_use_id]
    saved = finish_step(
        db, step_id, StepStatus.FAILED, error, error,
        checkpoint=_checkpoint_state(
            db, run["id"], context, completed, checkpoint_last_after_tool(tool_name),
        ),
        extra_event=StepEvent.TOOL_FAILED,
        extra_payload={"step_id": step_id, "tool_name": tool_name, "status": ToolCallStatus.FAILED},
    )
    if saved is None:
        return None
    _log_tool_failure(
        run, step_id=step_id, tool_call_id="", tool_name=tool_name,
        error_code=str(error.get("code") or ""), executed=executed,
        retryable=bool(error.get("retryable")), latency_ms=0,
    )
    return context, completed


def _after_tool_note(
    db: Database,
    run: dict[str, Any],
    context: list[dict[str, Any]],
    completed: list[str],
    tool_name: str,
    resume_note: str | None,
) -> Memory | None:
    if not resume_note:
        return context, completed, checkpoint_last_after_tool(tool_name), None, None
    context = [*context, {"role": "user", "content": resume_note}]
    if _save(db, run["id"], context, completed, LAST_INPUT) is None:
        return None
    return context, completed, LAST_INPUT, None, None


def _log_tool_failure(
    run: dict[str, Any],
    *,
    step_id: str,
    tool_call_id: str,
    tool_name: str,
    error_code: str,
    executed: bool,
    retryable: bool,
    latency_ms: int,
) -> None:
    """One line for a failed tool. Arguments, secrets, and token counts stay out."""
    log.info(
        "tool_call request_id=%s trace_id=%s run_id=%s org_id=%s user_id=%s outbox_id=%s "
        "step_id=%s tool_call_id=%s tool_name=%s error_code=%s executed=%s retryable=%s latency_ms=%s",
        "",
        run.get("trace_id") or "",
        run.get("id") or "",
        run.get("org_id") or "",
        run.get("user_id") or "",
        "",
        step_id,
        tool_call_id,
        tool_name,
        error_code,
        executed,
        retryable,
        latency_ms,
    )


def _request_never_left(exc: BaseException) -> bool:
    """True when the socket was refused before the request could be sent."""
    if isinstance(exc, (ConnectionRefusedError, ConnectionAbortedError, smtplib.SMTPConnectError)):
        return True
    return getattr(exc, "errno", None) in {
        errno.ECONNREFUSED, errno.ECONNABORTED, errno.EHOSTUNREACH, errno.ENETUNREACH,
    }


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)

def _pause(
    db: Database,
    run: dict[str, Any],
    context: list[dict[str, Any]],
    completed: list[str],
    call: Any,
    turn: AssistantTurn,
    latency_ms: int,
    provider: str,
    model_name: str,
) -> str | None:
    args = call.arguments if isinstance(call.arguments, dict) else {}
    step_id = _record_model_step(db, run, context, turn, latency_ms, provider, model_name, None, call.name, args)
    if step_id is None:
        return None
    if _save(db, run["id"], context, completed, checkpoint_last_before_tool(call.name), step_id) is None:
        return None
    parked = mark_waiting_for_user(db, run["id"], {"tool_name": call.name, "tool_use_id": call.id})
    return step_id if parked is not None else None

def _record_model_step(
    db: Database,
    run: dict[str, Any],
    context: list[dict[str, Any]],
    turn: AssistantTurn | None,
    latency_ms: int,
    provider: str,
    model_name: str,
    output: dict[str, Any] | None,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
) -> str | None:
    if tool_name is None:
        step_type, name, step_input = StepType.MODEL, step_name(StepType.MODEL, None), {}
    else:
        step_type, name, step_input = StepType.TOOL, step_name(StepType.TOOL, tool_name), tool_args or {}
    step = insert_step(db, run["id"], step_type, name, step_input)
    if step is None:
        return None
    response = _assistant_message(turn) if turn is not None else _last_assistant(context)
    prompt_tokens = int(getattr(turn, "prompt_tokens", 0) or 0) if turn is not None else 0
    completion_tokens = int(getattr(turn, "completion_tokens", 0) or 0) if turn is not None else 0
    recorded = insert_model_call(
        db, run["id"], step["id"],
        provider=provider, model=model_name, request={"messages": _request_messages(context)},
        response=response, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        latency_ms=latency_ms, status=StepStatus.SUCCEEDED,
    )
    if recorded is None:
        return None
    if output is not None and finish_step(db, step["id"], StepStatus.SUCCEEDED, output) is None:
        return None
    return step["id"]

def _decide(decide, context, tools) -> AssistantTurn:
    turn = decide(context, tools.schemas())
    if not isinstance(turn, AssistantTurn):
        raise PermanentRunError("orchestrator returned an unexpected value")
    return turn

def _stopped(db: Database, run_id: str, owner: str) -> bool:
    fresh = find_agent_run_by_id(db, run_id)
    if fresh is None or fresh["status"] != RunStatus.RUNNING:
        return True
    if fresh.get("cancel_requested_at") is not None:
        return True
    expires = fresh.get("lease_expires_at")
    if fresh.get("lease_owner") != owner or expires is None or expires <= utcnow():
        raise LeaseLost(f"lease lost for {run_id}")
    return False

def _checkpoint_state(
    db: Database,
    run_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
    last: str,
    open_step_id: str | None = None,
    last_report: str | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {CONTEXT_KEY: context, COMPLETED_TOOL_CALLS_KEY: completed, LAST_KEY: last}
    if open_step_id:
        state["open_step_id"] = open_step_id
    if last_report is None:
        previous = latest_checkpoint(db, run_id)
        saved = previous["state_json"].get("last_report") if previous else None
        last_report = saved if isinstance(saved, str) else ""
    if last_report.strip():
        state["last_report"] = last_report.strip()
    return state


def _save(
    db: Database,
    run_id: str,
    context: list[dict[str, Any]],
    completed: list[str],
    last: str,
    open_step_id: str | None = None,
    last_report: str | None = None,
) -> dict[str, Any] | None:
    return append_checkpoint(
        db, run_id, _checkpoint_state(db, run_id, context, completed, last, open_step_id, last_report),
    )

def _event(db: Database, run: dict[str, Any], event_type: StepEvent, payload: dict[str, Any]) -> None:
    append_run_event(db, run["org_id"], run["id"], event_type, payload)

def _resume_skipped(db: Database, run: dict[str, Any]) -> bool:
    """The latest resume decides. Anything other than skip still runs the tool."""
    events = list_run_events(db, run["org_id"], run["id"]) or []
    decision = None
    for event in events:
        if event["event_type"] != "run.resumed":
            continue
        incoming = event["payload_json"].get("input") if isinstance(event["payload_json"], dict) else None
        raw = incoming.get("decision") if isinstance(incoming, dict) else None
        if raw in ("skip", "confirm"):
            decision = raw
    return decision == "skip"

def _resume_note(db: Database, run: dict[str, Any], checkpoint_at: Any) -> str | None:
    events = list_run_events(db, run["org_id"], run["id"]) or []
    note = None
    for event in events:
        if event["event_type"] != "run.resumed" or event["created_at"] <= checkpoint_at:
            continue
        incoming = event["payload_json"].get("input") if isinstance(event["payload_json"], dict) else None
        message = incoming.get("message") if isinstance(incoming, dict) else None
        if isinstance(message, str) and message.strip():
            note = message
    return note

def _input_report(run: dict[str, Any]) -> str:
    """The report body supplied with this run. Later checkpoints copy it forward."""
    raw = run.get("input_json")
    report = raw.get("last_report") if isinstance(raw, dict) else None
    return report if isinstance(report, str) else ""

def _checkpoint_report(db: Database, run_id: str) -> str:
    row = latest_checkpoint(db, run_id)
    if row is None:
        return ""
    stored = row["state_json"].get("last_report")
    return stored.strip() if isinstance(stored, str) else ""

def _user_text(run_input: dict[str, Any]) -> str:
    message = run_input.get("message") if isinstance(run_input, dict) else None
    if isinstance(message, str) and message.strip():
        return message
    return json.dumps(run_input, ensure_ascii=False)

def _assistant_message(turn: AssistantTurn) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if turn.content:
        blocks.append({"type": "text", "text": turn.content})
    for call in turn.tool_calls:
        blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
    return {"role": "assistant", "content": blocks}

def _tool_result_message(tool_use_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": result}]}

def _last_tool_use(context: list[dict[str, Any]], tool_name: str | None = None) -> dict[str, Any] | None:
    found = _assistant_tool_uses(context)
    if tool_name:
        for block in reversed(found):
            if block.get("name") == tool_name:
                return block
        return None
    return found[-1] if found else None

def _last_assistant(context: list[dict[str, Any]]) -> dict[str, Any]:
    for message in reversed(context):
        if message.get("role") == "assistant":
            return message
    return {"role": "assistant", "content": []}

def _request_messages(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index in range(len(context) - 1, -1, -1):
        if context[index].get("role") == "assistant":
            return context[:index]
    return context

def _assistant_text(context: list[dict[str, Any]]) -> str:
    content = _last_assistant(context).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
