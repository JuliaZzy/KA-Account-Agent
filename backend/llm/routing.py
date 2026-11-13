"""Versioned model routing policy.

The policy chooses a primary model and optional fallback for a model purpose. It
does not contain secrets or prompt text; runtime decisions are written to Run
events by ``llm.calls`` so they remain visible in the existing trace.

Cheap short text uses official ``qwen3.7-flash``. Structured JSON, tools, vision,
and longer report prose use official ``deepseek-flash``. Report review and revise
are the exception: they take Qwen first. There is no campus path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)

ROUTING_POLICY_VERSION = "model-routing-v1"


class RouteDecision(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"


class RouteReason(StrEnum):
    POLICY_PRIMARY = "policy_primary"
    PREVIOUS_CANDIDATE_FAILED = "previous_candidate_failed"
    PROVIDER_ERROR = "provider_error"
    MODEL_CAPACITY = "model_capacity"
    INVALID_JSON = "invalid_json"
    QUALITY_GATE_FAILED = "quality_gate_failed"


@dataclass(frozen=True)
class RouteCandidate:
    provider: str
    model: str


QWEN_FLASH = RouteCandidate("qwen", "qwen3.7-flash")
DEEPSEEK_FLASH = RouteCandidate("deepseek", "deepseek-flash")


@dataclass(frozen=True)
class RoutePolicy:
    task: str
    primary: RouteCandidate
    fallbacks: tuple[RouteCandidate, ...] = ()
    max_tokens: int | None = None
    max_model_calls: int | None = None
    quality_gate: str = "response_parsed"
    fallback_on: tuple[str, ...] = ()

    @property
    def candidates(self) -> tuple[RouteCandidate, ...]:
        return (self.primary, *self.fallbacks)


@dataclass(frozen=True)
class RunBudget:
    """Safety-net caps on the run row. Purpose policies still cap one task."""

    max_model_calls: int | None = None
    max_tokens: int | None = None


# Prompt plus completion. Purpose policies cap accepted completion tokens separately.
RUN_BUDGETS: dict[str, RunBudget] = {
    "chat": RunBudget(max_model_calls=8, max_tokens=32_000),
    "open": RunBudget(max_model_calls=2, max_tokens=8_000),
}


def run_budget_for(run_type: str | None) -> RunBudget:
    """Return the run-level cap written at create time. Unknown types stay uncapped."""
    return RUN_BUDGETS.get(str(run_type or ""), RunBudget())


def _cheap_text(task: str, *, max_tokens: int = 1600) -> RoutePolicy:
    return RoutePolicy(
        task=task,
        primary=QWEN_FLASH,
        fallbacks=(DEEPSEEK_FLASH,),
        max_tokens=max_tokens,
        max_model_calls=2,
        quality_gate="response_parsed",
        fallback_on=("provider_error", "model_capacity", "invalid_json"),
    )


def _structured(
    task: str, *, max_tokens: int = 1200, max_model_calls: int = 2, quality_gate: str = "response_parsed",
) -> RoutePolicy:
    return RoutePolicy(
        task=task,
        primary=DEEPSEEK_FLASH,
        fallbacks=(QWEN_FLASH,),
        max_tokens=max_tokens,
        max_model_calls=max_model_calls,
        quality_gate=quality_gate,
        fallback_on=("provider_error", "model_capacity", "invalid_json", "quality_gate_failed"),
    )


ROUTES: dict[str, RoutePolicy] = {
    "orchestrator.plan": RoutePolicy(
        task="orchestrator.plan",
        primary=QWEN_FLASH,
        fallbacks=(DEEPSEEK_FLASH,),
        max_tokens=1200,
        max_model_calls=2,
        quality_gate="valid_task_plan",
        fallback_on=("provider_error", "model_capacity", "invalid_json", "quality_gate_failed"),
    ),
    "orchestrator.answer": RoutePolicy(
        task="orchestrator.answer",
        primary=QWEN_FLASH,
        fallbacks=(DEEPSEEK_FLASH,),
        max_tokens=1600,
        max_model_calls=2,
        quality_gate="non_empty_answer",
        fallback_on=("provider_error", "model_capacity"),
    ),
    "session.title": _cheap_text("session.title", max_tokens=800),
    "translate": _cheap_text("translate"),
    "po.reply": _cheap_text("po.reply"),
    "po.resolve": _cheap_text("po.resolve"),
    "po.claim": _cheap_text("po.claim"),
    "brand.query": _cheap_text("brand.query"),
    "brand.answer": RoutePolicy(
        task="brand.answer",
        primary=QWEN_FLASH,
        fallbacks=(DEEPSEEK_FLASH,),
        max_tokens=1600,
        max_model_calls=2,
        quality_gate="non_empty_answer",
        fallback_on=("provider_error", "model_capacity"),
    ),
    "memory.fold": _cheap_text("memory.fold"),
    "contract.select": _cheap_text("contract.select"),
    "contract.reflect": _cheap_text("contract.reflect"),
    "run.answer": RoutePolicy(
        task="run.answer",
        primary=QWEN_FLASH,
        fallbacks=(DEEPSEEK_FLASH,),
        max_tokens=1600,
        max_model_calls=2,
        quality_gate="non_empty_answer",
        fallback_on=("provider_error", "model_capacity"),
    ),
    "po.extract": _structured("po.extract", quality_gate="valid_po_slots"),
    "contract.extract": _structured("contract.extract"),
    # Catch-all if a report call forgets a graded purpose. Graded caps are per purpose, not a
    # shared 24. Writes ~7, review ~6 for two rounds, optional (plan/charts/wizards) ~4–8.
    "report": _structured("report", max_tokens=24_000, max_model_calls=24),
    "report.write": _structured("report.write", max_tokens=16_000, max_model_calls=12),
    "report.review": RoutePolicy(
        task="report.review",
        primary=QWEN_FLASH,
        fallbacks=(DEEPSEEK_FLASH,),
        max_tokens=12_000,
        max_model_calls=8,
        quality_gate="response_parsed",
        fallback_on=("provider_error", "model_capacity", "invalid_json", "quality_gate_failed"),
    ),
    "report.optional": _structured("report.optional", max_tokens=12_000, max_model_calls=12),
    "code.fix": _structured("code.fix"),
    "data.react": RoutePolicy(
        task="data.react", primary=DEEPSEEK_FLASH, fallbacks=(QWEN_FLASH,),
        max_tokens=8_000, max_model_calls=8,
        quality_gate="response_parsed",
        fallback_on=("provider_error", "model_capacity", "invalid_json"),
    ),
    "session.tools": _structured("session.tools"),
    "run.decide": _structured("run.decide"),
}


def policy_for(
    purpose: str | None,
    *,
    requested_model: str = "",
    requested_provider: str = "",
) -> RoutePolicy | None:
    """Return a policy for every named purpose without changing unconfigured models."""
    task = str(purpose or "")
    if not task:
        return None
    policy = ROUTES.get(task)
    if policy is not None:
        return policy
    # Unknown named purpose: keep the caller's model, fall back across official brands.
    fallback_by_provider = {
        "deepseek": QWEN_FLASH,
        "qwen": DEEPSEEK_FLASH,
        "glm": QWEN_FLASH,
    }
    fallback = fallback_by_provider.get(requested_provider)
    return RoutePolicy(
        task=task,
        primary=RouteCandidate(requested_provider or "unknown", requested_model),
        fallbacks=(fallback,) if fallback is not None else (),
        quality_gate="response_parsed",
        fallback_on=("provider_error", "model_capacity", "invalid_json"),
    )


def quality_check(policy: RoutePolicy, body: Any) -> tuple[bool, str]:
    """Apply cheap deterministic gates before accepting a routed response."""
    if policy.quality_gate == "response_parsed":
        # `_interpret` already rejected malformed JSON before this function runs.
        return True, "response_parsed"
    if policy.quality_gate == "non_empty_answer":
        return (bool(str(body or "").strip()), "non_empty_answer")
    if policy.quality_gate == "valid_task_plan":
        if not isinstance(body, dict) or not isinstance(body.get("tasks"), list):
            return False, "tasks_not_array"
        items = body["tasks"]
        ids = [str(item.get("id") or "") for item in items if isinstance(item, dict)]
        if (
            len(ids) != len(items)
            or any(not item_id for item_id in ids)
            or len(set(ids)) != len(ids)
        ):
            return False, "invalid_task_id"
        known = set(ids)
        for item in items:
            # `kind` is derived from the name by code; an older plan may still carry one.
            if not isinstance(item, dict) or item.get("kind") not in {None, "", "agent", "tool"}:
                return False, "invalid_task_kind"
            if not str(item.get("name") or "").strip():
                return False, "empty_task_name"
            dependencies = item.get("depends_on") or []
            if not isinstance(dependencies, list):
                return False, "unknown_dependency"
            dependency_ids = [str(dep) for dep in dependencies]
            if any(dep not in known for dep in dependency_ids):
                return False, "unknown_dependency"
        # Cycles stay for make_plan.reject_plan → InvalidPlan(plan_cycle).
        # Rejecting them here would fall back to another model (often an empty
        # plan in tests) and the run would succeed without a plan_cycle failure.
        return True, "valid_task_plan"
    if policy.quality_gate == "valid_po_slots":
        if not isinstance(body, dict) or not isinstance(body.get("line_items"), list):
            return False, "line_items_not_array"
        if not isinstance(body.get("reply_language"), str) or not body["reply_language"].strip():
            return False, "missing_reply_language"
        return True, "valid_po_slots"
    reason = f"unknown_quality_gate:{policy.quality_gate}"
    log.warning("quality gate rejected: task=%s gate=%s", policy.task, policy.quality_gate)
    return False, reason


def route_payload(
    policy: RoutePolicy,
    candidate: RouteCandidate,
    *,
    index: int,
    decision: RouteDecision,
    reason: RouteReason,
    reason_detail: str | None = None,
    fallback_from: str | None = None,
) -> dict[str, Any]:
    """Build non-sensitive trace metadata for one routing decision."""
    return {
        "route_task": policy.task,
        "policy_version": ROUTING_POLICY_VERSION,
        "selected_provider": candidate.provider,
        "selected_model": candidate.model,
        "candidate_index": index,
        "decision": decision.value,
        "reason": reason.value,
        "reason_detail": reason_detail,
        "fallback_from": fallback_from,
        "max_tokens": policy.max_tokens,
        "max_model_calls": policy.max_model_calls,
        "quality_gate": policy.quality_gate,
    }
