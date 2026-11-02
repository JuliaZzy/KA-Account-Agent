"""Closed names for one harness iteration.
`agent_runs.status` is the run state machine. One loop iteration writes one
`agent_steps` row, and this module is the only place that chooses that row's
name. Agents do not invent labels.

`RunStatus` and `OutboxStatus` live here as well. The repository imports
`Database`, and `Database` is built from the modules that write these values,
so those modules import the enums from this file.

A tool iteration is named after the registered tool. A model iteration is
always `model`. Which agent owns the run stays on `agent_runs.run_type`， 
and that column only holds `RunType`: a purchase, a contract, a report, a data
query, a brand answer, a code fix, a repair, a reflection, a report review,
a report revision, or one desk chat turn.
"""

from __future__ import annotations

import re
from enum import StrEnum

# `web_search`, and maybe later `po.lookup_sku`. Dots separate a domain from the tool. 
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")

# Listeners wake on this channel. The payload is the run id and nothing else.
RUN_EVENT_CHANNEL = "run_events"

class RunType(StrEnum):
    PO = "po"
    CONTRACT = "contract"
    REPORT = "report"
    DATA = "data"
    CODE = "code"
    BRAND = "brand"
    FIX = "fix"
    REFLECT = "reflect"
    REVIEW = "review"
    REVISE = "revise"
    CHAT = "chat"

class StepType(StrEnum):
    MODEL = "model"
    TOOL = "tool"

class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

class ToolCallStatus(StrEnum):
    """`tool_calls.status`. `skipped_duplicate` is a loop result, not a column value."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RESPONSE_LOST = "response_lost"

class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

class OutboxStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    QUEUED = "queued"

class ToolErrorCode(StrEnum):
    VALIDATION = "TOOL_VALIDATION_ERROR"
    NOT_FOUND = "TOOL_NOT_FOUND"
    PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
    IDEMPOTENCY_REQUIRED = "TOOL_IDEMPOTENCY_REQUIRED"
    TIMEOUT = "TOOL_TIMEOUT"
    RATE_LIMITED = "TOOL_RATE_LIMITED"
    UPSTREAM = "TOOL_UPSTREAM_ERROR"

def tool_error(code: ToolErrorCode | str, message: str, *, retryable: bool = False) -> dict[str, str | bool]:
    """The shape stored on a failed tool call. `code` is a `ToolErrorCode` value."""
    return {"code": str(code), "message": message, "retryable": retryable}


class EntryErrorCode(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    COST_BUDGET_EXCEEDED = "COST_BUDGET_EXCEEDED"

class StepEvent(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_WAITING_FOR_USER = "run.waiting_for_user"
    RUN_REQUEUED_BY_SWEEPER = "run.requeued_by_sweeper"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_RETRY_SCHEDULED = "run.retry_scheduled"
    RUN_RESUMED = "run.resumed"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_ENQUEUED = "run.enqueued"
    RUN_QUEUE_STALE = "run.queue_stale"
    RUN_WAITING_REMINDER = "run.waiting_reminder"
    RUN_BUDGET_WARNING = "run.budget_warning"
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_MANUAL_REVIEW = "tool.manual_review"

MODEL_STEP_NAME = StepType.MODEL.value

# Pushed to the chat page. Trace still returns every stored event.
PUBLIC_RUN_EVENTS = frozenset({
    StepEvent.RUN_CREATED,
    StepEvent.RUN_STARTED,
    StepEvent.STEP_STARTED,
    StepEvent.STEP_COMPLETED,
    StepEvent.MODEL_STARTED,
    StepEvent.MODEL_COMPLETED,
    StepEvent.TOOL_STARTED,
    StepEvent.TOOL_COMPLETED,
    StepEvent.TOOL_FAILED,
    StepEvent.TOOL_MANUAL_REVIEW,
    StepEvent.RUN_WAITING_FOR_USER,
    StepEvent.RUN_REQUEUED_BY_SWEEPER,
    StepEvent.RUN_COMPLETED,
    StepEvent.RUN_FAILED,
    StepEvent.RUN_CANCELLED,
})

# The page paints `display` and does not keep a second copy of these sentences.
EVENT_DISPLAY: dict[str, str] = {
    StepEvent.RUN_CREATED: "已开始",
    StepEvent.MODEL_STARTED: "正在思考",
    StepEvent.RUN_WAITING_FOR_USER: "等待确认",
    StepEvent.TOOL_MANUAL_REVIEW: "结果不确定，需要人确认",
    StepEvent.RUN_REQUEUED_BY_SWEEPER: "继续处理",
    StepEvent.RUN_COMPLETED: "回复结束",
    StepEvent.RUN_FAILED: "这次没有完成",
    StepEvent.RUN_CANCELLED: "已停止",
}

TOOL_STARTED_DISPLAY: dict[str, str] = {
    "po.draft": "正在起草采购单",
    "contract.read": "正在读合同",
    "report.draft": "正在写报告",
    "data.query": "正在查数",
    "brand.search": "正在查品牌资料",
    "web_search": "正在检索",
    "translate": "正在翻译",
    "exchange_rate": "正在查汇率",
    "send_email": "正在发信",
}

# Dropped when a row is read for the page. The stored payload keeps them.
PRIVATE_PAYLOAD_KEYS = frozenset({
    "purpose",
    "prompt",
    "token",
    "tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
})


def event_display(event_type: str, payload: dict) -> str | None:
    """The sentence stored on a public event. Callers that already set `display` win."""
    if event_type == StepEvent.TOOL_STARTED:
        name = payload.get("tool_name")
        if isinstance(name, str):
            return TOOL_STARTED_DISPLAY.get(name)
        return None
    return EVENT_DISPLAY.get(event_type)

# Keys inside `checkpoints.state_json`.
CONTEXT_KEY = "context"
COMPLETED_TOOL_CALLS_KEY = "completed_tool_calls"
LAST_KEY = "last"

# `last` before any model call, after the first user message is stored.
LAST_INPUT = "user"
# `last` after the final model text is stored. 
LAST_REPLY = "assistant:reply"

def step_name(step_type: StepType | str, tool_name: str | None) -> str:
    """The only way to choose `agent_steps.name`"""
    kind = step_type if isinstance(step_type, StepType) else StepType(step_type)
    if kind is StepType.MODEL:
        if tool_name is not None:
            raise ValueError("a model step has no tool name.")
        return MODEL_STEP_NAME
    if tool_name is None or tool_name == MODEL_STEP_NAME or _TOOL_NAME.fullmatch(tool_name) is None:
        raise ValueError(f"Invalid tool name: {tool_name}")
    return tool_name

def checkpoint_last_before_tool(tool_name: str) -> str:
    """Stored before tool runs."""
    return f"assistant:tool_use:{step_name(StepType.TOOL, tool_name)}"

def checkpoint_last_after_tool(tool_name: str) -> str:
    """Stored after the tool result is back in the context."""
    return f"tool_result:{step_name(StepType.TOOL, tool_name)}"