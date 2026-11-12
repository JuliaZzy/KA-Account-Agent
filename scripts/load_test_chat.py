"""Run a small end-to-end chat load test against the Compose API.

The workload is synthetic. It creates independent sessions, submits messages through the
real HTTP endpoint, and waits until the worker has written an assistant message back to each
session. It measures user-visible completion time; the Postgres ledger remains the source of
truth for run and tool metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from backend.llm.pricing import call_cost_cny, call_uncached_cost_cny

# A 100-request run leaves about 20 rows in one agent class. P99 there is the slowest point.
# Publish P99 only for a pooled sample of at least 300. Smaller slices report P50 and P95.
P99_MIN_SAMPLES = 300


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _latency(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99) if len(values) >= P99_MIN_SAMPLES else None,
    }


def summarize_usage_rows(rows: list[dict[str, Any]], *, requests: int = 0) -> dict[str, Any]:
    """Aggregate token usage, cache hit ratio, and CNY cost from model-call rows."""
    prompt_total = completion_total = 0
    hit_total = miss_total = creation_total = 0
    cached_prompt_total = 0
    has_cache_sample = False
    cost_cny = uncached_cny = chat_cost_cny = open_cost_cny = 0.0
    chat_runs: dict[str, dict[str, int]] = {}
    by_model: dict[tuple[str, str, str], dict[str, Any]] = {}

    for row in rows:
        run_type = str(row.get("run_type") or "chat")
        provider = str(row.get("provider") or "")
        model = str(row.get("model") or "")
        prompt = max(0, int(row.get("prompt_tokens") or 0))
        completion = max(0, int(row.get("completion_tokens") or 0))
        hit_raw = row.get("cache_hit_tokens")
        miss_raw = row.get("cache_miss_tokens")
        create_raw = row.get("cache_creation_tokens")
        created_at = row.get("created_at")

        prompt_total += prompt
        completion_total += completion
        if hit_raw is not None:
            has_cache_sample = True
            hit_val = max(0, int(hit_raw))
            create_val = max(0, int(create_raw or 0))
            miss_val = (
                max(0, int(miss_raw))
                if miss_raw is not None
                else max(0, prompt - hit_val - create_val)
            )
            hit_total += hit_val
            miss_total += miss_val
            creation_total += create_val
            cached_prompt_total += prompt

        cny = call_cost_cny(
            provider,
            model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_hit_tokens=hit_raw,
            cache_miss_tokens=miss_raw,
            cache_creation_tokens=create_raw,
            created_at=created_at,
        )
        uncached = call_uncached_cost_cny(
            provider,
            model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            created_at=created_at,
        )
        cost_cny += cny
        uncached_cny += uncached
        if run_type == "chat":
            chat_cost_cny += cny
            run_id = str(row.get("run_id") or "")
            if run_id:
                bucket = chat_runs.setdefault(run_id, {"prompt": 0, "completion": 0})
                bucket["prompt"] += prompt
                bucket["completion"] += completion
        elif run_type == "open":
            open_cost_cny += cny

        group = by_model.setdefault(
            (run_type, provider, model),
            {
                "run_type": run_type,
                "provider": provider,
                "model": model,
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
                "estimated_cost_cny": 0.0,
                "uncached_cost_cny": 0.0,
            },
        )
        group["calls"] += 1
        group["prompt_tokens"] += prompt
        group["completion_tokens"] += completion
        group["cache_hit_tokens"] += max(0, int(hit_raw or 0))
        group["cache_miss_tokens"] += (
            max(0, int(miss_raw))
            if miss_raw is not None
            else max(0, prompt - max(0, int(hit_raw or 0)) - max(0, int(create_raw or 0)))
        )
        group["estimated_cost_cny"] = round(group["estimated_cost_cny"] + cny, 6)
        group["uncached_cost_cny"] = round(group["uncached_cost_cny"] + uncached, 6)

    chat_prompts = [float(item["prompt"]) for item in chat_runs.values()]
    chat_completions = [float(item["completion"]) for item in chat_runs.values()]
    denom = max(requests, len(chat_runs), 1) if (requests or chat_runs) else 0
    return {
        "model_calls": len(rows),
        "unknown_usage_calls": sum(row.get("usage_source") == "unknown" for row in rows),
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "total_tokens": prompt_total + completion_total,
        "cache_hit_tokens": hit_total if has_cache_sample else None,
        "cache_miss_tokens": miss_total if has_cache_sample else None,
        "cache_creation_tokens": creation_total if has_cache_sample else None,
        "cache_hit_ratio": (
            round(hit_total / cached_prompt_total * 100, 2)
            if has_cache_sample and cached_prompt_total > 0
            else None
        ),
        "estimated_cost_cny": round(cost_cny, 6),
        "estimated_cost_cents": round(cost_cny * 100, 4),
        "uncached_cost_cny": round(uncached_cny, 6),
        "cache_savings_ratio": (
            round((1.0 - cost_cny / uncached_cny) * 100, 2)
            if uncached_cny > 0
            else None
        ),
        "per_request_cost_cny": round(cost_cny / denom, 6) if denom else None,
        "per_chat_run_cost_cny": round(chat_cost_cny / denom, 6) if denom else None,
        "per_open_run_cost_cny": round(open_cost_cny / denom, 6) if denom else None,
        "chat_prompt_tokens_p50": _percentile(chat_prompts, 0.50),
        "chat_prompt_tokens_p95": _percentile(chat_prompts, 0.95),
        "chat_completion_tokens_p50": _percentile(chat_completions, 0.50),
        "chat_completion_tokens_p95": _percentile(chat_completions, 0.95),
        "by_model": [by_model[key] for key in sorted(by_model)],
    }


def _ledger_usage(session_ids: list[str], *, requests: int, db: Any | None = None) -> dict[str, Any] | None:
    if not session_ids:
        return None
    try:
        from backend.db import Database

        rows = (db or Database()).session_usage_facts(session_ids)
    except Exception:
        return None
    if not rows:
        return None
    return summarize_usage_rows(rows, requests=requests)


def _seconds(value: float | None, *, tail: bool = False) -> str:
    if value is None:
        return f"未报（不足 {P99_MIN_SAMPLES} 条）" if tail else "没有样本"
    return f"{value} 秒"


def _message(template: str, index: int) -> str:
    return template.replace("{i}", str(index))


def _templates(args: argparse.Namespace) -> list[str]:
    if args.messages_file:
        templates = [
            line.strip() for line in args.messages_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if templates:
            return templates
        raise ValueError(f"{args.messages_file} contains no message templates")
    return [args.message_template]


async def _create_session(client: httpx.AsyncClient, base_url: str, partnership_id: str, user_id: str) -> str:
    response = await client.post(
        f"{base_url}/api/sessions",
        json={"partnership_id": partnership_id, "user_id": user_id},
    )
    response.raise_for_status()
    return str(response.json()["id"])


async def _wait_for_reply(
    client: httpx.AsyncClient, base_url: str, session_id: str, deadline: float,
) -> bool:
    while time.monotonic() < deadline:
        response = await client.get(f"{base_url}/api/sessions/{session_id}")
        response.raise_for_status()
        history = response.json().get("history") or []
        if any(isinstance(item, dict) and item.get("role") == "assistant" for item in history):
            return True
        await asyncio.sleep(0.25)
    return False


def _split_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _actors(args: argparse.Namespace) -> list[tuple[str, str]]:
    """(partnership_id, user_id) pairs cycled across requests."""
    partnerships = _split_ids(args.partnership_ids) or [args.partnership_id]
    users = _split_ids(args.user_ids) or [args.user_id]
    if args.partnership_ids and len(partnerships) == len(users):
        return list(zip(partnerships, users, strict=True))
    # Same org, many users (S2): one partnership × N users.
    if len(partnerships) == 1:
        return [(partnerships[0], user_id) for user_id in users]
    # Many orgs (S3): zip when lengths match; otherwise cartesian is too wide — require equal length.
    raise ValueError(
        "for multi-org runs pass matching --partnership-ids and --user-ids "
        f"(got {len(partnerships)} partnerships and {len(users)} users)"
    )


async def _one(
    client: httpx.AsyncClient,
    base_url: str,
    session_id: str,
    message: str,
    timeout_seconds: float,
    *,
    partnership_id: str,
    user_id: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {
        "session_id": session_id,
        "partnership_id": partnership_id,
        "user_id": user_id,
        "message": message,
    }
    phase = "enqueue"
    try:
        response = await client.post(
            f"{base_url}/api/sessions/{session_id}/message",
            json={"message": message},
        )
        result["enqueue_status"] = response.status_code
        result["enqueue_ms"] = round((time.perf_counter() - started) * 1000, 2)
        response.raise_for_status()
        run_id = response.json().get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("enqueue response has no run_id")
        result["run_id"] = run_id
        result["run_event"] = True
        phase = "poll"
        result["completed"] = await _wait_for_reply(
            client, base_url, session_id, time.monotonic() + timeout_seconds,
        )
        result["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        result["outcome"] = "completed" if result["completed"] else "client_timeout"
        if not result["completed"]:
            try:
                status = await client.get(
                    f"{base_url}/api/runs/{run_id}", headers={"X-User-Id": user_id},
                )
                status.raise_for_status()
                result["server_status_at_timeout"] = status.json().get("status")
            except (httpx.HTTPError, ValueError) as exc:
                result["status_error"] = type(exc).__name__
    except Exception as exc:
        result["completed"] = False
        result["outcome"] = "enqueue_error" if phase == "enqueue" else "poll_error"
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    result.setdefault("total_ms", round((time.perf_counter() - started) * 1000, 2))
    return result


_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


async def _settle_runs(
    db: Any, results: list[dict[str, Any]], *, timeout_seconds: float, interval_seconds: float,
) -> dict[str, Any]:
    """Follow only this experiment's sessions; never cancel or wait indefinitely."""
    session_ids = [str(row["session_id"]) for row in results if row.get("session_id")]
    expected = {str(row["run_id"]) for row in results if row.get("run_id")}
    deadline = time.monotonic() + timeout_seconds
    rows: list[dict[str, Any]] = []
    while True:
        rows = await asyncio.to_thread(db.session_run_facts, session_ids)
        found = {str(row["run_id"]) for row in rows}
        pending = [row for row in rows if row.get("status") not in _TERMINAL]
        missing = sorted(expected - found)
        if not pending and not missing:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(interval_seconds, remaining))
    by_id = {str(row["run_id"]): row for row in rows}
    by_session: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_session.setdefault(str(row.get("session_id") or ""), []).append(row)
    for result in results:
        run = by_id.get(str(result.get("run_id") or ""))
        if not run and not result.get("run_id"):
            matches = [
                row for row in by_session.get(str(result.get("session_id") or ""), [])
                if row.get("run_type") == "chat"
            ]
            if len(matches) == 1:
                run = matches[0]
                result["recovered_run_id"] = run["run_id"]
        if run:
            result["server_status_at_followup"] = run["status"]
            result["final_server_status"] = run["status"] if run["status"] in _TERMINAL else None
            result["final_server_error_code"] = run.get("error_code")
            if result.get("outcome") == "client_timeout" and run["status"] == "succeeded":
                result["final_outcome"] = "completed_after_timeout"
            elif result.get("outcome") == "client_timeout" and run["status"] in {"failed", "cancelled"}:
                result["final_outcome"] = f"server_{run['status']}"
            else:
                result["final_outcome"] = result.get("outcome")
    return {
        "scope": "experiment_sessions",
        "wait_limit_seconds": timeout_seconds,
        "status_counts": dict(Counter(str(row.get("status")) for row in rows)),
        "unfinished_count": sum(row.get("status") not in _TERMINAL for row in rows),
        "missing_run_ids": sorted(expected - set(by_id)),
        "runs": rows,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    wall_started = time.perf_counter()
    base_url = args.base_url.rstrip("/")
    templates = _templates(args)
    actors = _actors(args)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(args.request_timeout)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        slots: list[tuple[str, str, str]] = []
        for index in range(args.requests):
            partnership_id, user_id = actors[index % len(actors)]
            session_id = await _create_session(client, base_url, partnership_id, user_id)
            slots.append((session_id, partnership_id, user_id))
        semaphore = asyncio.Semaphore(args.concurrency)

        async def limited(
            index: int, session_id: str, partnership_id: str, user_id: str,
            scheduled: float | None = None,
        ) -> dict[str, Any]:
            async with semaphore:
                send_started = time.perf_counter()
                row = await _one(
                    client, base_url, session_id,
                    _message(templates[(index - 1) % len(templates)], index),
                    args.completion_timeout,
                    partnership_id=partnership_id,
                    user_id=user_id,
                )
                if scheduled is not None:
                    row["scheduled_offset_seconds"] = round(scheduled - schedule_started, 4)
                    row["actual_send_offset_seconds"] = round(send_started - schedule_started, 4)
                    row["generator_delay_seconds"] = round(max(0.0, send_started - scheduled), 4)
                return row

        schedule_started = time.perf_counter()
        if args.arrival_rate:
            pending: set[asyncio.Task[dict[str, Any]]] = set()
            tasks: list[asyncio.Task[dict[str, Any]]] = []
            dropped: list[dict[str, Any]] = []
            for i, (session_id, partnership_id, user_id) in enumerate(slots, 1):
                scheduled = schedule_started + (i - 1) * 60.0 / args.arrival_rate
                await asyncio.sleep(max(0.0, scheduled - time.perf_counter()))
                pending = {task for task in pending if not task.done()}
                if len(pending) >= args.max_pending:
                    dropped.append({
                        "session_id": session_id, "partnership_id": partnership_id,
                        "user_id": user_id, "completed": False, "outcome": "generator_dropped",
                        "scheduled_offset_seconds": round(scheduled - schedule_started, 4),
                    })
                    continue
                task = asyncio.create_task(limited(i, session_id, partnership_id, user_id, scheduled))
                pending.add(task)
                tasks.append(task)
            results = [*(await asyncio.gather(*tasks)), *dropped]
        else:
            results = await asyncio.gather(*(
                limited(i, session_id, partnership_id, user_id)
                for i, (session_id, partnership_id, user_id) in enumerate(slots, 1)
            ))
        measurement_seconds = time.perf_counter() - schedule_started

    completed = [row for row in results if row.get("completed")]
    completed_durations = [float(row["total_ms"]) / 1000 for row in completed]
    observed_durations = [float(row["total_ms"]) / 1000 for row in results if "total_ms" in row]
    observed = _latency(observed_durations)
    done = _latency(completed_durations)
    settle: dict[str, Any]
    usage = None
    db = None
    try:
        from backend.db import Database

        db = await asyncio.to_thread(Database)
        settle = await _settle_runs(
            db, results, timeout_seconds=args.settle_timeout, interval_seconds=args.settle_interval,
        )
        usage = await asyncio.to_thread(
            _ledger_usage,
            [str(row.get("session_id") or "") for row in results],
            requests=sum(row.get("outcome") != "generator_dropped" for row in results), db=db,
        )
    except Exception as exc:
        settle = {"scope": "experiment_sessions", "error": type(exc).__name__}
    finally:
        if db is not None:
            db.engine.dispose()
    outcomes = dict(Counter(str(row.get("outcome") or "unknown") for row in results))
    submitted = len(results) - outcomes.get("generator_dropped", 0)
    send_offsets = sorted(
        float(row["actual_send_offset_seconds"])
        for row in results if "actual_send_offset_seconds" in row
    )
    send_span = send_offsets[-1] - send_offsets[0] if len(send_offsets) > 1 else 0.0
    summary: dict[str, Any] = {
        "offered": len(results),
        "submitted": submitted,
        "completed": len(completed),
        "timeouts": outcomes.get("client_timeout", 0),
        "not_completed": submitted - len(completed),
        "outcomes": outcomes,
        "completion_rate": len(completed) / submitted * 100 if submitted else None,
        "measurement_seconds": round(measurement_seconds, 3),
        "actual_submit_rate_per_minute": (
            round(submitted * 60 / measurement_seconds, 3) if measurement_seconds else None
        ),
        "open_loop_send_rate_per_minute": (
            round((len(send_offsets) - 1) * 60 / send_span, 3) if send_span > 0 else None
        ),
        "completion_rate_per_minute": (
            round(len(completed) * 60 / measurement_seconds, 3) if measurement_seconds else None
        ),
        "observed_p50_seconds": observed["p50"],
        "observed_p95_seconds": observed["p95"],
        "observed_p99_seconds": observed["p99"],
        "observed_max_seconds": max(observed_durations, default=None),
        "completed_p50_seconds": done["p50"],
        "completed_p95_seconds": done["p95"],
        "completed_p99_seconds": done["p99"],
        "completed_max_seconds": max(completed_durations, default=None),
    }
    if usage is not None:
        usage["scope_note"] = (
            "Succeeded model calls recorded at follow-up snapshot; "
            "failed calls may be unbilled or missing usage."
        )
        usage["snapshot_incomplete"] = bool(
            settle.get("error") or settle.get("unfinished_count") or settle.get("missing_run_ids")
        )
        summary["usage"] = usage
    return {
        "workload": {
            "base_url": base_url,
            "partnership_id": args.partnership_id,
            "user_id": args.user_id,
            "partnership_ids": sorted({pair[0] for pair in actors}),
            "user_ids": sorted({pair[1] for pair in actors}),
            "requests": args.requests,
            "concurrency": args.concurrency,
            "mode": "open_loop" if args.arrival_rate else "closed_loop",
            "arrival_rate_per_minute": args.arrival_rate,
            "max_pending": args.max_pending if args.arrival_rate else None,
            "request_timeout_seconds": args.request_timeout,
            "completion_timeout_seconds": args.completion_timeout,
            "started_at_utc": started_at.isoformat(),
            "ended_at_utc": datetime.now(UTC).isoformat(),
            "wall_seconds_including_followup": round(time.perf_counter() - wall_started, 3),
            "message_templates": templates,
        },
        "summary": summary,
        "results": results,
        "follow_up": settle,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--partnership-id", required=True)
    parser.add_argument("--user-id", required=True, help="primary user; also used when --user-ids is omitted")
    parser.add_argument(
        "--user-ids",
        help="comma-separated users to cycle (same org smoke S2). Example: user-a,user-b,user-c",
    )
    parser.add_argument(
        "--partnership-ids",
        help="comma-separated partnerships aligned with --user-ids (multi-org smoke S3)",
    )
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--arrival-rate", type=float, help="open-loop offered requests per minute")
    parser.add_argument("--max-pending", type=int, default=100, help="open-loop active and waiting limit")
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--completion-timeout", type=float, default=180)
    parser.add_argument(
        "--settle-timeout", type=float, default=30, help="max seconds to follow experiment runs",
    )
    parser.add_argument("--settle-interval", type=float, default=2, help="seconds between ledger snapshots")
    parser.add_argument(
        "--message-template",
        default="你好，这是第 {i} 条合成压测消息，请回复一句确认。",
    )
    parser.add_argument(
        "--messages-file", type=Path, help="one message template per line; cycles across requests",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/experiments/exports/load-test-result.json"),
    )
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.concurrency > args.requests:
        parser.error("requests must be >= 1 and concurrency must be between 1 and requests")
    if args.settle_timeout < 0 or args.settle_interval <= 0:
        parser.error("settle-timeout must be >= 0 and settle-interval must be > 0")
    if args.arrival_rate is not None and (args.arrival_rate <= 0 or args.max_pending < 1):
        parser.error("arrival-rate and max-pending must be positive")
    try:
        _actors(args)
    except ValueError as exc:
        parser.error(str(exc))

    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    usage_info = summary.get("usage")
    cost_part = ""
    if isinstance(usage_info, dict):
        cost_part = (
            f"Token {usage_info['prompt_tokens']} in / {usage_info['completion_tokens']} out，"
            f"花费 ¥{usage_info['estimated_cost_cny']:.4f}"
            f"（单条 ¥{usage_info['per_request_cost_cny']:.6f}）；"
        )
    print(
        f"合成压测：{summary['completed']}/{summary['submitted']} 完成 "
        f"({summary['completion_rate']:.1f}%)；"
        f"成功 P50 {_seconds(summary['completed_p50_seconds'])}，"
        f"P95 {_seconds(summary['completed_p95_seconds'])}，"
        f"P99 {_seconds(summary['completed_p99_seconds'], tail=True)}；"
        f"{cost_part}"
        f"结果已写入 {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
