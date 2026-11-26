"""Parse the orchestrator PLAN. The model only emits tasks; workers execute them."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ..db import Database
from ..llm import calls as model_calls
from ..llm.calls import ModelOutputError
from ..memory import attach, prompt_turns
from ..services.run_names import StepStatus, StepType, step_name
from ..tools import all_schemas
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
    """The PLAN graph still cannot be dispatched after the correction budget.

    The message is what the desk shows. `code` is the stable reason for the trace.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# Draft, two repairs, one shorter plan, then two more repairs.
PLAN_FOLLOWUPS = ("repair", "repair", "degrade", "repair", "repair")
PLAN_ATTEMPTS = len(PLAN_FOLLOWUPS) + 1
PLAN_FAILURE_MESSAGE = "系统出错，请稍后再试。"
PLAN_UNUSABLE_NOTE = "计划没有生成，不要声称已经查过或已经起草。"
UNKNOWN_TOOL_ERROR = "工具或代理不存在"
DANGLING_TASK_ERROR = "依赖的任务没能执行"


@dataclass(frozen=True)
class PlanIssue:
    """One problem in a plan. Fatal problems reject the plan; the rest can be dropped."""

    code: str
    message: str
    fatal: bool = False
    task_id: str | None = None


@dataclass
class PlanDraft:
    """What the parent dispatches after the planner has had its attempts.

    `rejected` are tasks that stayed illegal. They are already failure envelopes.
    `unusable` means the document itself never became a plan: the answer must say so.
    """

    tasks: list[PlanTask]
    rejected: list[dict[str, Any]] = field(default_factory=list)
    unusable: bool = False
    note: str | None = None


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


def legal_names() -> frozenset[str]:
    """Agent names the planner is allowed to write, plus every tool schema name."""
    names = set(AGENT_NAMES) | set(AGENT_ALIASES)
    for schema in all_schemas():
        function = schema.get("function") if isinstance(schema, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return frozenset(names)


def _has_cycle(ids: list[str], dependencies: dict[str, list[str]]) -> bool:
    """Iterative Kahn. A node left with an inbound edge is part of a cycle."""
    indegree = {task_id: 0 for task_id in ids}
    outgoing: dict[str, list[str]] = {task_id: [] for task_id in ids}
    for task_id in ids:
        for dep in dependencies.get(task_id, []):
            if dep not in indegree:
                continue
            indegree[task_id] += 1
            outgoing[dep].append(task_id)
    ready = [task_id for task_id, degree in indegree.items() if degree == 0]
    seen = 0
    while ready:
        current = ready.pop()
        seen += 1
        for child in outgoing[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return seen != len(ids)


def inspect_plan(raw: Any, *, names: frozenset[str] | None = None) -> list[PlanIssue]:
    """Every problem in this plan. Nothing is returned early once `tasks` is a list.

    A fence is not a problem: `parse_json_loose` has already removed it. An unparseable
    body never reaches here. `tasks` not being an array, and a cycle, are fatal. A bad
    task, an unknown name, or a missing field is reported and can be dropped later.
    """
    allowed = names if names is not None else legal_names()
    if not isinstance(raw, dict):
        return [PlanIssue("invalid_json", "输出不是合法 JSON 对象", fatal=True)]
    items = raw.get("tasks")
    if not isinstance(items, list):
        return [PlanIssue("tasks_not_array", "tasks 必须是数组", fatal=True)]

    issues: list[PlanIssue] = []
    ids: list[str] = []
    seen: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, item in enumerate(items):
        label = f"tasks[{index}]"
        if not isinstance(item, dict):
            issues.append(PlanIssue("incomplete", f"{label} 不是对象"))
            continue
        task_id = item.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            issues.append(PlanIssue("incomplete", f"{label} 缺少 id"))
            task_id = ""
        else:
            task_id = task_id.strip()
            if task_id in seen:
                issues.append(PlanIssue("duplicate_id", f"id {task_id!r} 重复", task_id=task_id))
            else:
                seen.add(task_id)
                ids.append(task_id)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(PlanIssue("incomplete", f"{label} 缺少 name", task_id=task_id or None))
        elif name.strip() not in allowed:
            issues.append(PlanIssue(
                "unknown_name",
                f"任务 {task_id or label} 的 name {name.strip()!r} 不是已注册的代理或工具",
                task_id=task_id or None,
            ))
        if "kind" in item and item.get("kind") not in ("", "agent", "tool"):
            issues.append(PlanIssue(
                "invalid_kind",
                f"任务 {task_id or label} 的 kind 只能是 agent、tool，或者不写",
                task_id=task_id or None,
            ))
        if "input" not in item or not isinstance(item.get("input"), dict):
            issues.append(PlanIssue(
                "missing_input", f"任务 {task_id or label} 缺少 input 对象", task_id=task_id or None,
            ))
        raw_deps = item.get("depends_on") if "depends_on" in item else None
        clean: list[str] = []
        if not isinstance(raw_deps, list):
            issues.append(PlanIssue(
                "missing_depends_on",
                f"任务 {task_id or label} 缺少 depends_on 数组",
                task_id=task_id or None,
            ))
        else:
            bad_dep = False
            for dep in raw_deps:
                if not isinstance(dep, str) or not dep.strip():
                    bad_dep = True
                    continue
                clean.append(dep.strip())
            if bad_dep:
                issues.append(PlanIssue(
                    "missing_depends_on",
                    f"任务 {task_id or label} 的 depends_on 含有空值或非字符串",
                    task_id=task_id or None,
                ))
        if task_id and task_id in ids and task_id not in dependencies:
            dependencies[task_id] = clean
    known = set(ids)
    for task_id, deps in dependencies.items():
        for dep in deps:
            if dep not in known:
                issues.append(PlanIssue(
                    "unknown_dependency",
                    f"任务 {task_id} 的 depends_on 指向不存在的任务 {dep}",
                    task_id=task_id,
                ))
    if ids and _has_cycle(ids, dependencies):
        issues.append(PlanIssue("plan_cycle", "depends_on 成环", fatal=True))
    return issues


def _failed_task(task_id: str, name: str, error: str, *, kind: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "kind": "agent" if kind == "agent" else "tool",
        "name": name,
        "status": "failed",
        "ok": False,
        "error": error,
        "payload": {},
    }


def executable_tasks(
    raw: dict[str, Any], *, names: frozenset[str] | None = None,
) -> tuple[list[PlanTask], dict[str, int], list[dict[str, Any]]]:
    """Keep tasks that can run. Unknown names become failure envelopes.

    A task whose dependency was dropped becomes a failure envelope too, so the
    answer can say that step did not run. Missing `input` becomes `{}` and a
    missing `depends_on` becomes `[]` only when the task itself is kept.
    """
    allowed = names if names is not None else legal_names()
    items = raw.get("tasks") if isinstance(raw, dict) else None
    dropped = {
        "unknown_agent": 0, "unknown_tool": 0, "duplicate_id": 0, "incomplete": 0, "dangling": 0,
    }
    if not isinstance(items, list):
        return [], dropped, []
    seen: set[str] = set()
    rejected: list[dict[str, Any]] = []
    parsed: list[PlanTask] = []
    for item in items:
        if not isinstance(item, dict):
            dropped["incomplete"] += 1
            continue
        task_id = item.get("id")
        name = item.get("name")
        if not isinstance(task_id, str) or not task_id.strip():
            dropped["incomplete"] += 1
            continue
        if not isinstance(name, str) or not name.strip():
            dropped["incomplete"] += 1
            continue
        task_id = task_id.strip()
        name = name.strip()
        if task_id in seen:
            dropped["duplicate_id"] += 1
            continue
        if name not in allowed:
            kind = "agent" if name.endswith("_agent") or item.get("kind") == "agent" else "tool"
            if kind == "agent":
                dropped["unknown_agent"] += 1
            else:
                dropped["unknown_tool"] += 1
            rejected.append(_failed_task(task_id, name, UNKNOWN_TOOL_ERROR, kind=kind))
            continue
        agent = AGENT_ALIASES.get(name) or (name if name in AGENT_NAMES else None)
        if agent is not None:
            kind, name = "agent", agent
        else:
            kind = "tool"
        payload = item.get("input") if isinstance(item.get("input"), dict) else {}
        raw_deps = item.get("depends_on")
        if isinstance(raw_deps, list):
            deps = [dep.strip() for dep in raw_deps if isinstance(dep, str) and dep.strip()]
        else:
            deps = []
        seen.add(task_id)
        parsed.append(PlanTask(id=task_id, kind=kind, name=name, input=dict(payload), depends_on=deps))
    kept = parsed
    while True:
        present = {task.id for task in kept}
        dangling = [task for task in kept if any(dep not in present for dep in task.depends_on)]
        if not dangling:
            break
        dropped["dangling"] += len(dangling)
        for task in dangling:
            rejected.append(_failed_task(task.id, task.name, DANGLING_TASK_ERROR, kind=task.kind))
        gone = {task.id for task in dangling}
        kept = [task for task in kept if task.id not in gone]
    return _one_po(kept), dropped, rejected


def _followup(kind: str, issues: list[PlanIssue], previous: str, *, truncated: bool) -> str:
    """The next user turn. A cut-off reply asks for a shorter plan, not a rewrite."""
    if truncated:
        head = (
            "上一次输出被长度截断，没有写完。不要把同样长的计划再写一遍。"
            "请精简：只保留这一轮必须做的任务，JSON 要短。只返回一个 JSON 对象，不要解释。"
        )
    elif kind == "degrade":
        head = (
            "上面的计划改了两轮仍然不能执行。请降级：只保留这一轮必须做的、名字合法的任务，"
            "去掉不存在的工具和代理，拆掉成环的依赖。计划要短。只返回一个 JSON 对象，不要解释。"
        )
    else:
        return _repair_note(issues, previous)
    lines = "\n".join(f"- {issue.message}" for issue in issues)
    shown = previous.strip() or "（空）"
    return f"{head}\n{lines}\n\n上一次输出：\n{shown[:6000]}"


def _repair_note(issues: list[PlanIssue], previous: str) -> str:
    lines = "\n".join(f"- {issue.message}" for issue in issues)
    shown = previous.strip() or "（空）"
    return (
        "上一次输出不能执行。下面是全部问题，请一次改完。只返回一个 JSON 对象，不要解释。\n"
        f"{lines}\n\n"
        "上一次输出：\n"
        f"{shown[:6000]}"
    )


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

async def make_plan(
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
    checkpoint: Callable[[list[PlanTask], list[dict[str, Any]], str | None], dict[str, Any]] | None = None,
) -> PlanDraft:
    """Ask official qwen3.7-flash for this turn's tasks. An empty task list can still answer.

    Two repairs, then a shorter plan, then two more repairs. A cycle, unparseable
    JSON, or a cut-off reply leaves the plan unusable: the parent answers without
    claiming work was done, and a dead letter keeps the case. Unknown names stay
    in the result list as failures so the answer can say they did not run.
    """
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
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    system = attach(plan_system(), memory or "")
    parsed: dict[str, Any] | None = None
    issues: list[PlanIssue] = []
    previous = ""
    repairs = 0
    caught: Exception | None = None
    code: str | None = None
    unusable = False
    try:
        for attempt in range(PLAN_ATTEMPTS):
            repairs = attempt
            truncated = False
            try:
                result = await model_calls.qwen_json_async(
                    system=system, messages=messages, schema=plan_schema(), temperature=0, client=llm,
                    db=db, run_id=run_id, step_id=step_id, purpose="orchestrator.plan",
                )
                truncated = model_calls.last_finish_reason() == "length"
            except ModelOutputError as err:
                parsed = None
                previous = err.raw or str(err)
                truncated = err.finish_reason == "length" or model_calls.last_finish_reason() == "length"
                if truncated:
                    issues = [PlanIssue("plan_truncated", "输出被长度截断", fatal=True)]
                else:
                    issues = [PlanIssue("invalid_json", "输出不是合法 JSON 对象", fatal=True)]
            else:
                if isinstance(result, dict):
                    parsed = result
                    previous = json.dumps(result, ensure_ascii=False)
                    issues = inspect_plan(result)
                else:
                    parsed = None
                    previous = str(result)
                    issues = [PlanIssue("invalid_json", "输出不是合法 JSON 对象", fatal=True)]
            if not issues or attempt == PLAN_ATTEMPTS - 1:
                break
            messages.append({"role": "assistant", "content": previous[:6000] or "（空）"})
            messages.append({"role": "user", "content": _followup(
                PLAN_FOLLOWUPS[attempt], issues, previous, truncated=truncated and parsed is None,
            )})
        fatal = next((issue for issue in issues if issue.fatal), None)
        if fatal is not None:
            code = fatal.code
            unusable = True
    except Exception as err:
        caught = err
    tasks: list[PlanTask] = []
    rejected: list[dict[str, Any]] = []
    dropped: dict[str, int] | None = None
    note: str | None = None
    if caught is None and not unusable and parsed is not None:
        tasks, notes, rejected = executable_tasks(parsed)
        tasks = scheduled(tasks)
        tasks = _normalize_data_contract(tasks, message)
        dropped = _present_drops(notes)
        problem = graph_error(tasks)
        if problem is not None:
            code = "plan_cycle" if "cycle" in problem else "plan_unknown_dependency"
            unusable = True
            tasks = []
            rejected = []
    elif caught is None and parsed is None:
        code = code or "invalid_json"
        unusable = True
    if unusable:
        note = PLAN_UNUSABLE_NOTE
        tasks = []
        rejected = []
        if db is not None and run_id:
            db.record_plan_failure(run_id, {
                "code": code,
                "message": note,
                "issues": [
                    {"code": issue.code, "message": issue.message, "task_id": issue.task_id}
                    for issue in issues
                ],
            })
    if db is not None and run_id and step_id:
        body: dict[str, Any] = {"tasks": [task.as_dict() for task in tasks]}
        if dropped:
            body["dropped"] = dropped
        if rejected:
            body["rejected"] = rejected
        if repairs:
            body["repairs"] = repairs
        issue_body = [
            {"code": issue.code, "message": issue.message, "task_id": issue.task_id}
            for issue in issues
        ]
        saved = None
        if checkpoint is not None and caught is None:
            saved = checkpoint(tasks, rejected, note)
        failed = caught is not None or unusable
        db.finish_step(
            step_id,
            StepStatus.FAILED if failed else StepStatus.SUCCEEDED,
            None if failed else body,
            None if not failed else {
                "message": str(caught) if caught is not None else note,
                **({} if code is None else {"code": code}),
                **body,
                **({"issues": issue_body} if issue_body else {}),
            },
            saved,
        )
    if caught is not None:
        raise caught
    return PlanDraft(tasks=tasks, rejected=rejected, unusable=unusable, note=note)


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
