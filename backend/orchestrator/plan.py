"""Parse the orchestrator PLAN. The model only emits tasks; workers execute them."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..memory import attach, prompt_turns
from ..services.run_names import StepStatus, StepType, step_name
from ..tools import all_schemas
from .prompt import PLAN

AGENT_NAMES = frozenset({"po", "contract", "report", "brand", "data"})
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
                        "kind": {"type": "string", "enum": ["agent", "tool"]},
                        "name": {"type": "string"},
                        "input": {"type": "object"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "kind", "name", "input", "depends_on"],
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
    items = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    out: list[PlanTask] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or "").strip()
        kind = str(item.get("kind") or "").strip()
        name = str(item.get("name") or "").strip()
        if not task_id or task_id in seen:
            continue
        if kind not in ("agent", "tool") or not name:
            continue
        if kind == "agent" and name not in AGENT_NAMES:
            continue
        payload = item.get("input") if isinstance(item.get("input"), dict) else {}
        deps = [str(dep) for dep in (item.get("depends_on") or []) if isinstance(dep, str) and dep]
        seen.add(task_id)
        out.append(PlanTask(id=task_id, kind=kind, name=name, input=dict(payload), depends_on=deps))
    return _one_po(out)


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
    """Drop a data task a report lists as its own input. The draft queries itself."""
    hinted = {dep for task in tasks if task.kind == "agent" and task.name == "report" for dep in task.depends_on}
    if not hinted:
        return list(tasks)
    return [
        task for task in tasks
        if not (task.kind == "agent" and task.name == "data" and task.id in hinted)
    ]


def ready_wave(tasks: list[PlanTask], finished: set[str]) -> list[PlanTask]:
    pending = [task for task in tasks if task.id not in finished]
    known = {task.id for task in tasks}
    ready = [
        task for task in pending
        if all(dep not in known or dep in finished for dep in task.depends_on)
    ]
    return ready


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
) -> list[PlanTask]:
    """Ask official qwen3.7-flash for this turn's tasks. An empty list means ANSWER only."""
    tools = _tool_lines()
    user = (
        f"=== 未完成采购单 ===\n{'有' if open_po else '无'}\n\n"
        f"=== 附件 ===\n{', '.join(attachments) or '（无）'}\n\n"
        f"=== 最近对话 ===\n{_history_block(history, folded_through)}\n\n"
        f"=== 消息 ===\n{message[:4000] or '（无文字）'}\n\n"
        f"=== 可用函数 ===\n{tools}"
    )
    step_id = None
    if db is not None and run_id:
        step = db.insert_step(run_id, StepType.MODEL, step_name(StepType.MODEL, None), {"purpose": "orchestrator.plan"})
        step_id = None if step is None else step["id"]
    raw: dict[str, Any] = {}
    caught: Exception | None = None
    try:
        parsed = model_calls.qwen_json(
            system=attach(PLAN, memory or ""),
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
    if db is not None and run_id and step_id:
        db.finish_step(
            step_id,
            StepStatus.FAILED if caught is not None else StepStatus.SUCCEEDED,
            None if caught is not None else {"tasks": [task.as_dict() for task in parse_tasks(raw)]},
            {"message": str(caught)} if caught is not None else None,
        )
    if caught is not None:
        raise caught
    return scheduled(parse_tasks(raw))


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
            lines.append(f"{role}: {content[:500]}")
    return "\n".join(lines) or "（无）"


def _tool_lines() -> str:
    names: list[str] = []
    for schema in all_schemas():
        func = schema.get("function") if isinstance(schema, dict) else None
        if not isinstance(func, dict):
            continue
        name = func.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return ", ".join(names) or "web_search, translate, exchange_rate, send_email"
