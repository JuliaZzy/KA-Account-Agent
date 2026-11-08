"""Redis 只放三件不属于账本的事。

1. 会话锁用来避免确认按钮和仍在起草的这一轮同时写同一条会话。
2. 一波入队任务的计数和暂存，归零后唤起 owner。账本仍是 Postgres 的 step / tool_call / checkpoint。
3. 汇率缓存，统一货币在一段时间内的重复请求直接走缓存回答，不需要多次调用 Frankfurter。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from redis import Redis

from .config import redis_url

# 会话锁120秒过期
LOCK_TTL_MS = 120_000
# 并行任务结果900秒过期
TURN_TTL_S = 900
# Frankfurter 每天发布一次汇率，所以一个货币对缓存一天
FX_TTL_S = 86_400
# 获取会话锁的超时时间
_ACQUIRE_WAIT_S = 90

_RELEASE = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""

_REFRESH = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("pexpire", KEYS[1], ARGV[2])
else
  return 0
end
"""

# One hash field per task, then decrement the counter once, in one round trip.
_STORE = """
local first = redis.call("hexists", KEYS[1], ARGV[1]) == 0
redis.call("hset", KEYS[1], ARGV[1], ARGV[2])
local left = redis.call("get", KEYS[2])
if first and left then
  left = redis.call("decr", KEYS[2])
end
redis.call("expire", KEYS[1], ARGV[3])
redis.call("expire", KEYS[2], ARGV[3])
return left
"""

_client: Redis | None = None
_client_lock = threading.Lock()


def client() -> Redis:
    global _client
    with _client_lock:
        if _client is None:
            _client = Redis.from_url(
                redis_url(), decode_responses=True, socket_connect_timeout=2, socket_timeout=5,
            )
        return _client


def _lock_key(session_id: str) -> str:
    return f"pd:lock:session:{session_id}"


def _turn_key(session_id: str, turn_id: str) -> str:
    return f"pd:turn:{session_id}:{turn_id}"


def _left_key(session_id: str, turn_id: str) -> str:
    return f"pd:turn:{session_id}:{turn_id}:left"


def _fx_key(base: str, target: str) -> str:
    return f"pd:fx:{base}:{target}"


@contextmanager
def hold_session(session_id: str) -> Iterator[None]:
    """Block until this chat is free, then keep the lock while the caller runs.

    A confirm and a generation both write one session row. The lock lives in
    Redis so a second API process waits too, without holding a database
    connection for the whole model call.
    """
    token = _acquire(session_id)
    stop = threading.Event()
    beater = threading.Thread(
        target=_beat, args=(session_id, token, stop), name="session-lock", daemon=True,
    )
    beater.start()
    try:
        yield
    finally:
        stop.set()
        beater.join(timeout=1)
        _release(session_id, token)


def _acquire(session_id: str) -> str:
    token = uuid.uuid4().hex
    key = _lock_key(session_id)
    deadline = time.monotonic() + _ACQUIRE_WAIT_S
    while True:
        if client().set(key, token, nx=True, px=LOCK_TTL_MS):
            return token
        if time.monotonic() >= deadline:
            raise TimeoutError(f"session {session_id} is still busy")
        time.sleep(0.02)


def _beat(session_id: str, token: str, stop: threading.Event) -> None:
    while not stop.wait(LOCK_TTL_MS / 3000):
        try:
            client().eval(_REFRESH, 1, _lock_key(session_id), token, str(LOCK_TTL_MS))
        except Exception:
            return


def _release(session_id: str, token: str) -> None:
    try:
        client().eval(_RELEASE, 1, _lock_key(session_id), token)
    except Exception:
        return


def begin_turn(session_id: str, turn_id: str, task_ids: list[str]) -> None:
    """PLAN 里同一波并行子任务的 redis 倒计时"""
    pipe = client().pipeline()
    # 清理掉上一次的倒计时
    pipe.delete(_turn_key(session_id, turn_id), _left_key(session_id, turn_id))
    # 剩余计数设置为 len(task_ids)
    pipe.set(_left_key(session_id, turn_id), len(task_ids), ex=TURN_TTL_S)
    # 过期时间设置为 TURN_TTL_S
    pipe.expire(_turn_key(session_id, turn_id), TURN_TTL_S)
    pipe.execute()


def store_result(session_id: str, turn_id: str, task_id: str, result: dict[str, Any]) -> int:
    """Write one task's result. Returns how many tasks are still out."""
    raw = json.dumps(result, ensure_ascii=False, default=str)
    left = client().eval(
        _STORE, 2, _turn_key(session_id, turn_id), _left_key(session_id, turn_id),
        task_id, raw, str(TURN_TTL_S),
    )
    return int(left)


def load_results(session_id: str, turn_id: str) -> dict[str, dict[str, Any]]:
    raw = client().hgetall(_turn_key(session_id, turn_id))
    out: dict[str, dict[str, Any]] = {}
    for task_id, body in raw.items():
        if not body:
            continue
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            out[str(task_id)] = parsed
    return out


def drop_turn(session_id: str, turn_id: str) -> None:
    try:
        client().delete(_turn_key(session_id, turn_id), _left_key(session_id, turn_id))
    except Exception:
        return


def fx_get(base: str, target: str) -> dict[str, Any] | None:
    try:
        raw = client().get(_fx_key(base, target))
    except Exception:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def fx_put(base: str, target: str, *, rate: float, date: str | None, source: str) -> None:
    try:
        client().set(
            _fx_key(base, target),
            json.dumps({"rate": rate, "date": date, "source": source}, ensure_ascii=False),
            ex=FX_TTL_S,
        )
    except Exception:
        return


def fx_drop(base: str, target: str) -> None:
    try:
        client().delete(_fx_key(base, target))
    except Exception:
        return
