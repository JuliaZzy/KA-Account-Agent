"""Parse the orchestrator PLAN. The model only emits tasks; workers execute them."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..memory import attach, prompt_turns
from ..services.run_names import StepStatus, StepType, step_name
from .prompt import plan_system

AGENT_NAMES = frozenset({"po", "contract", "report", "brand", "data"})
# What the planner writes. The suffix is how code tells an agent from a tool, so the model never
# has to state a `kind` that could disagree with the name.
AGENT_ALIASES = {f"{name}_agent": name for name in AGENT_NAMES}
AGENT_TOOLS = {
    "po": "po.draft",
    "contract": "contract.read",
    "report": "report.draft",
    "data": "data.query",
    "brand": "brand.search",
}
AGENT_RUN_TYPES = {
    "po": "po",
    "contract": "contract",
    "report": "report",
    "data": "data",
    "brand": "brand",
}
RESULT_STATUSES = frozenset({"succeeded", "failed", "skipped"})


class InvalidPlan(ValueError):
    """The PLAN graph cannot be dispatched. Retrying the model will not fix it."""


@dataclass
class PlanTask:
    id: str
    kind: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_schema() -> dict[str, Any]:
    return {
        "name": "orchestrator_plan", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "tasks": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "input": {"type": "object"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "name", "input", "depends_on"],
                }},
            },
            "required": ["tasks"],
        },
    }


def tool_name_for(task: PlanTask) -> str:
    if task.kind == "agent":
        return AGENT_TOOLS[task.name]
    return task.name


def run_type_for(task: PlanTask) -> str:
    if task.kind == "agent":
        return AGENT_RUN_TYPES[task.name]
    return "tool"


def parse_tasks(raw: Any) -> list[PlanTask]:
    tasks, _dropped, _unknown_ids = _accept(raw)
    return tasks


def _accept(raw: Any) -> tuple[list[PlanTask], dict[str, int], set[str]]:
    """Keep the runnable tasks and count what the model emitted that we refused.

    `unknown_agent` is a name outside the registry. A later task that still
    depends on that id fails the whole plan; the count is how often the model
    does it, including calls that still succeed because nothing depended on it.
    """
    items = raw.get("tasks") if isinstance(raw, dict) else None
    dropped = {"unknown_agent": 0, "duplicate_id": 0, "incomplete": 0, "extra_po": 0}
    unknown_ids: set[str] = set()
    if not isinstance(items, list):
        return [], dropped, unknown_ids
    out: list[PlanTask] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            dropped["incomplete"] += 1
            continue
        task_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        # A `kind` from an older plan (checkpoints, replays) is read only to reject a bad value or an
        # unknown agent; whether the task is an agent is decided by its name alone.
        hint = str(item.get("kind") or "").strip()
        if not task_id:
            dropped["incomplete"] += 1
            continue
        if task_id in seen:
            dropped["duplicate_id"] += 1
            continue
        if not name or hint not in ("", "agent", "tool"):
            dropped["incomplete"] += 1
            continue
        agent = AGENT_ALIASES.get(name) or (name if name in AGENT_NAMES else None)
        if agent is not None:
            kind, name = "agent", agent
        elif name.endswith("_agent") or hint == "agent":
            dropped["unknown_agent"] += 1
            unknown_ids.add(task_id)
            continue
        else:
            kind = "tool"
        payload = item.get("input") if isinstance(item.get("input"), dict) else {}
        deps = [str(dep) for dep in (item.get("depends_on") or []) if isinstance(dep, str) and dep]
        seen.add(task_id)
        out.append(PlanTask(id=task_id, kind=kind, name=name, input=dict(payload), depends_on=deps))
    before = len(out)
    out = _one_po(out)
    dropped["extra_po"] += before - len(out)
    return out, dropped, unknown_ids


def _present_drops(dropped: dict[str, int]) -> dict[str, int] | None:
    kept = {key: value for key, value in dropped.items() if value}
    return kept or None


def _one_po(tasks: list[PlanTask]) -> list[PlanTask]:
    kept: list[PlanTask] = []
    saw_po = False
    for task in tasks:
        if task.kind == "agent" and task.name == "po":
            if saw_po:
                continue
            saw_po = True
        kept.append(task)
    return kept


def scheduled(tasks: list[PlanTask]) -> list[PlanTask]:
    """Drop a data task a report lists as its own input. The draft queries itself.

    The dropped id is removed from `depends_on` too. Leaving it would look like
    a dependency on a task that is no longer in the plan.
    """
    hinted = {dep for task in tasks if task.kind == "agent" and task.name == "report" for dep in task.depends_on}
    if not hinted:
        return list(tasks)
    dropped = {
        task.id for task in tasks
        if task.kind == "agent" and task.name == "data" and task.id in hinted
    }
    kept: list[PlanTask] = []
    for task in tasks:
        if task.id in dropped:
            continue
        deps = [dep for dep in task.depends_on if dep not in dropped]
        if deps != task.depends_on:
            task = PlanTask(
                id=task.id, kind=task.kind, name=task.name,
                input=dict(task.input), depends_on=deps,
            )
        kept.append(task)
    return kept


# A chart request is recognised in code as well as by the planner: the planner sometimes drops
# `chart` when the sentence is phrased as a comparison ("对比…，用柱状图"), and then the answer
# has numbers but no picture. A whole report (report task) draws its own charts.
_CHART_ASK = re.compile(r"饼图|折线图|柱状图|柱形图|条形图|曲线图|趋势图|占比图|面积图|散点图|图表|画.{0,8}图|做.{0,8}图|\b(chart|plot|graph)\b", re.I)


def _normalize_data_contract(tasks: list[PlanTask], message: str = "") -> list[PlanTask]:
    """Fill the deterministic delivery mode on every planned data task."""
    wants_chart = bool(_CHART_ASK.search(message or "")) and not any(
        task.kind == "agent" and task.name == "report" for task in tasks
    )
    data = [task for task in tasks if task.kind == "agent" and task.name == "data"]
    only_data = len(tasks) == 1 and len(data) == 1
    data_ids = {task.id for task in data}
    normalized: list[PlanTask] = []
    for task in tasks:
        if task.id not in data_ids:
            normalized.append(task)
            continue
        has_downstream = any(task.id in other.depends_on for other in tasks)
        payload = dict(task.input)
        if payload.get("chart") or wants_chart:
            payload["chart"] = True
        else:
            payload.pop("chart", None)
        payload["deliver"] = "answer" if only_data and not task.depends_on and not has_downstream else "data"
        columns = payload.get("columns", payload.get("required_columns"))
        if isinstance(columns, list):
            payload["columns"] = [str(column).strip() for column in columns if str(column).strip()]
        else:
            payload["columns"] = []
        normalized.append(PlanTask(
            id=task.id, kind=task.kind, name=task.name, input=payload, depends_on=list(task.depends_on),
        ))
    return normalized


def graph_error(tasks: list[PlanTask]) -> str | None:
    """A dependency must name a task in this plan, and the edges must be acyclic."""
    known = {task.id for task in tasks}
    missing = [
        f"{task.id} -> {dep}"
        for task in tasks
        for dep in task.depends_on
        if dep not in known
    ]
    if missing:
        return "depends_on points at an unknown task: " + ", ".join(missing)
    by_id = {task.id: task for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        for dep in by_id[task_id].depends_on:
            if walk(dep):
                return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    for task_id in by_id:
        if walk(task_id):
            return "depends_on contains a cycle"
    return None


def reject_plan(tasks: list[PlanTask], unknown_ids: set[str]) -> tuple[str, str] | None:
    """Message and a stable code. None when the graph can be dispatched."""
    message = graph_error(tasks)
    if message is None:
        return None
    if "cycle" in message:
        return message, "plan_cycle"
    known = {task.id for task in tasks}
    missing = {dep for task in tasks for dep in task.depends_on if dep not in known}
    if missing & unknown_ids:
        return message, "plan_depends_on_unknown_agent"
    return message, "plan_unknown_dependency"


def ready_wave(tasks: list[PlanTask], finished: set[str]) -> list[PlanTask]:
    """Tasks whose dependencies are all finished. An unknown dependency is not finished."""
    return [
        task for task in tasks
        if task.id not in finished and all(dep in finished for dep in task.depends_on)
    ]

def make_plan(
    llm: Any,
    *,
    message: str,
    attachments: list[str],
    history: list[dict[str, Any]],
    open_po: bool,
    folded_through: int = 0,
    db: Database | None = None,
    run_id: str | None = None,
    memory: str | None = None,
    checkpoint: Callable[[list[PlanTask]], dict[str, Any]] | None = None,
) -> list[PlanTask]:
    """Ask official qwen3.7-flash for this turn's tasks. An empty list means ANSWER only."""
    user = (
        f"=== 未完成采购单 ===\n{'有' if open_po else '无'}\n\n"
        f"=== 附件 ===\n{', '.join(attachments) or '（无）'}\n\n"
        f"=== 最近对话 ===\n{_history_block(history, folded_through)}\n\n"
        f"=== 消息 ===\n{message[:2000] or '（无文字）'}"
    )
    step_id = None
    if db is not None and run_id:
        step = db.insert_step(run_id, StepType.MODEL, step_name(StepType.MODEL, None), {"purpose": "orchestrator.plan"})
        step_id = None if step is None else step["id"]
    raw: dict[str, Any] = {}
    caught: Exception | None = None
    try:
        parsed = model_calls.qwen_json(
            system=attach(plan_system(), memory or ""),
            messages=[{"role": "user", "content": user}],
            schema=plan_schema(), temperature=0, client=llm,
            db=db, run_id=run_id, step_id=step_id, purpose="orchestrator.plan",
        )
        if isinstance(parsed, dict):
            raw = parsed
        else:
            caught = ValueError("model did not return a JSON object")
    except Exception as err:
        caught = err
    tasks: list[PlanTask] = []
    dropped: dict[str, int] | None = None
    code: str | None = None
    if caught is None:
        tasks, notes, unknown_ids = _accept(raw)
        tasks = scheduled(tasks)
        tasks = _normalize_data_contract(tasks, message)
        dropped = _present_drops(notes)
        rejected = reject_plan(tasks, unknown_ids)
        if rejected is not None:
            problem, code = rejected
            caught = InvalidPlan(problem)
    if db is not None and run_id and step_id:
        body = {"tasks": [task.as_dict() for task in tasks]}
        if dropped:
            body["dropped"] = dropped
        db.finish_step(
            step_id,
            StepStatus.FAILED if caught is not None else StepStatus.SUCCEEDED,
            None if caught is not None else body,
            None if caught is None else {
                "message": str(caught), **({} if code is None else {"code": code}), **body,
            },
            None if caught is not None or checkpoint is None else checkpoint(tasks),
        )
    if caught is not None:
        raise caught
    return tasks


def _history_block(history: list[dict[str, Any]], folded_through: int = 0) -> str:
    """Turns still outside the summary. Folded lines stay in the summary, not here."""
    lines: list[str] = []
    for message in prompt_turns(history, folded_through):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(message.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content[:1500]}")
    return "\n".join(lines) or "（无）"
