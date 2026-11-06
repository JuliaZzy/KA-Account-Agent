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
from pathlib import Path
from typing import Any

import httpx


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


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


async def _one(
    client: httpx.AsyncClient,
    base_url: str,
    session_id: str,
    message: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {"session_id": session_id, "message": message}
    try:
        response = await client.post(
            f"{base_url}/api/sessions/{session_id}/message",
            json={"message": message},
        )
        result["enqueue_status"] = response.status_code
        result["enqueue_ms"] = round((time.perf_counter() - started) * 1000, 2)
        response.raise_for_status()
        result["run_event"] = "run" in response.text
        result["completed"] = await _wait_for_reply(
            client, base_url, session_id, time.monotonic() + timeout_seconds,
        )
    except Exception as exc:
        result["completed"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    result["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url.rstrip("/")
    templates = _templates(args)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(args.request_timeout)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        sessions = [
            await _create_session(client, base_url, args.partnership_id, args.user_id)
            for _ in range(args.requests)
        ]
        semaphore = asyncio.Semaphore(args.concurrency)

        async def limited(index: int, session_id: str) -> dict[str, Any]:
            async with semaphore:
                return await _one(
                    client, base_url, session_id, _message(templates[(index - 1) % len(templates)], index),
                    args.completion_timeout,
                )

        results = await asyncio.gather(*(limited(i, sid) for i, sid in enumerate(sessions, 1)))

    completed = [row for row in results if row.get("completed")]
    completed_durations = [float(row["total_ms"]) / 1000 for row in completed]
    observed_durations = [float(row["total_ms"]) / 1000 for row in results]
    return {
        "workload": {
            "base_url": base_url,
            "partnership_id": args.partnership_id,
            "user_id": args.user_id,
            "requests": args.requests,
            "concurrency": args.concurrency,
            "message_templates": templates,
        },
        "summary": {
            "submitted": len(results),
            "completed": len(completed),
            "timeouts": len(results) - len(completed),
            "completion_rate": len(completed) / len(results) * 100 if results else None,
            "observed_p50_seconds": _percentile(observed_durations, 0.50),
            "observed_p95_seconds": _percentile(observed_durations, 0.95),
            "observed_max_seconds": max(observed_durations, default=None),
            "completed_p50_seconds": _percentile(completed_durations, 0.50),
            "completed_p95_seconds": _percentile(completed_durations, 0.95),
            "completed_max_seconds": max(completed_durations, default=None),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://api:8000")
    parser.add_argument("--partnership-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--completion-timeout", type=float, default=180)
    parser.add_argument(
        "--message-template",
        default="你好，这是第 {i} 条合成压测消息，请回复一句确认。",
    )
    parser.add_argument("--messages-file", type=Path, help="one message template per line; cycles across requests")
    parser.add_argument("--output", type=Path, default=Path(".load-test-result.json"))
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1 or args.concurrency > args.requests:
        parser.error("requests must be >= 1 and concurrency must be between 1 and requests")

    report = asyncio.run(run(args))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    print(
        f"合成压测：{summary['completed']}/{summary['submitted']} 完成 "
        f"({summary['completion_rate']:.1f}%)；"
        f"观测 P95 {summary['observed_p95_seconds']} 秒；"
        f"成功请求 P95 {summary['completed_p95_seconds']} 秒；"
        f"结果已写入 {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
