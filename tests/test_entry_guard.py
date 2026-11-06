"""Occupancy gates are one member per holder, reclaimed on the 300s window."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace

from backend.redis_store import client
from backend.services import entry_guard
from backend.services.entry_guard import (
    Lease,
    _acquire,
    _count,
    _open,
    _renew,
    acquire_model,
    acquire_run,
    acquire_tool,
    limits,
)


def _key() -> str:
    return f"gate:test:{uuid.uuid4().hex}"


def _now_ms() -> int:
    sec, usec = client().time()
    return int(sec) * 1000 + int(usec) // 1000


def _close(leases: list[Lease | None], *keys: str) -> None:
    for lease in leases:
        if lease is not None:
            lease.release()
    if keys:
        client().delete(*keys)


def test_a_full_gate_keeps_the_holder_it_already_has():
    key = _key()
    first = second = third = None
    try:
        first = _acquire([(key, 1)])
        assert first is not None
        members = set(client().zrange(key, 0, -1))
        second = _acquire([(key, 1)])
        assert second is None
        assert client().zcard(key) == 1
        assert set(client().zrange(key, 0, -1)) == members
        first.release()
        first.release()
        assert client().exists(key) == 0
        third = _acquire([(key, 1)])
        assert third is not None
    finally:
        _close([first, second, third], key)


def test_release_drops_only_that_holder():
    key = _key()
    first = second = None
    try:
        first = _acquire([(key, 2)])
        second = _acquire([(key, 2)])
        assert first is not None and second is not None
        first.release()
        first.release()
        assert client().zrange(key, 0, -1) == [second._holder]
    finally:
        _close([first, second], key)


def test_a_stale_member_is_swept_and_a_fresh_one_is_kept():
    key = _key()
    lease = None
    try:
        client().zadd(key, {"zombie": 0, "fresh": _now_ms()})
        assert _count(key) == 1
        assert client().zrange(key, 0, -1) == ["fresh"]
        client().delete(key)
        client().zadd(key, {"zombie": 0})
        lease = _acquire([(key, 1)])
        assert lease is not None
        assert client().zrange(key, 0, -1) == [lease._holder]
    finally:
        _close([lease], key)


def test_a_leftover_counter_becomes_a_set():
    key = _key()
    lease = None
    try:
        client().set(key, "9")
        lease = _acquire([(key, 1)])
        assert lease is not None
        assert client().type(key) == "zset"
        assert client().zcard(key) == 1
    finally:
        _close([lease], key)


def test_a_limit_of_zero_takes_no_seat():
    key = _key()
    lease = _acquire([(key, 0), (key, -1)])
    try:
        assert lease is not None
        assert client().exists(key) == 0
        lease.release()
        lease.release()
    finally:
        _close([lease], key)


def test_a_partial_acquire_rolls_back_only_this_attempt():
    left, right = _key(), _key()
    keeper = blocker = failed = None
    try:
        keeper = _acquire([(left, 2)])
        blocker = _acquire([(right, 1)])
        failed = _acquire([(left, 2), (right, 1)])
        assert keeper is not None and blocker is not None and failed is None
        assert client().zrange(left, 0, -1) == [keeper._holder]
        assert client().zrange(right, 0, -1) == [blocker._holder]
    finally:
        _close([keeper, blocker, failed], left, right)


def test_renew_moves_the_score_and_reports_a_lost_member():
    key = _key()
    lease = _acquire([(key, 1)])
    assert lease is not None
    try:
        before = float(client().zscore(key, lease._holder))
        time.sleep(0.05)
        assert _renew(key, lease._holder) is True
        after = client().zscore(key, lease._holder)
        assert after is not None and float(after) > before
        client().zrem(key, lease._holder)
        assert _renew(key, lease._holder) is False
    finally:
        _close([lease], key)


def test_lease_marks_all_seats_lost_when_renewal_fails(monkeypatch):
    key = _key()
    monkeypatch.setattr(entry_guard, "_BEAT_S", 0.01)
    monkeypatch.setattr(entry_guard, "_renew", lambda _keys, _holder: False)
    lease = _acquire([(key, 1)])
    assert lease is not None
    try:
        deadline = time.monotonic() + 1
        while not lease.lost and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lease.lost is True
    finally:
        lease.release()
        client().delete(key)


def test_model_gate_uses_limits_model(monkeypatch):
    brand = f"test-{uuid.uuid4().hex}"
    base = limits()
    monkeypatch.setattr(entry_guard, "limits", lambda: replace(base, model=2))
    key = f"gate:model:{brand}"
    first = second = third = None
    try:
        first = acquire_model(brand)
        second = acquire_model(brand)
        third = acquire_model(brand)
        assert first is not None and second is not None and third is None
        assert client().zcard(key) == 2
    finally:
        _close([first, second, third], key)


def test_tool_gate_uses_the_group_limit(monkeypatch):
    org = f"test-{uuid.uuid4().hex}"
    base = limits()
    monkeypatch.setattr(
        entry_guard, "limits", lambda: replace(base, read_tool=1, side_effect=2),
    )
    read_key = f"gate:tool:{org}:read_tool"
    side_key = f"gate:tool:{org}:side_effect"
    held: list[Lease | None] = []
    try:
        held.append(acquire_tool(org, "read_tool"))
        held.append(acquire_tool(org, "read_tool"))
        assert held[0] is not None and held[1] is None
        held.append(acquire_tool(org, "side_effect"))
        held.append(acquire_tool(org, "side_effect"))
        held.append(acquire_tool(org, "side_effect"))
        assert held[2] is not None and held[3] is not None and held[4] is None
        assert client().zcard(read_key) == 1
        assert client().zcard(side_key) == 2
    finally:
        _close(held, read_key, side_key)


def test_acquire_run_passes_the_three_limits(monkeypatch):
    base = limits()
    monkeypatch.setattr(
        entry_guard, "limits",
        lambda: replace(base, global_running=7, org_running=5, user_running=2),
    )
    monkeypatch.setattr(entry_guard, "_room", lambda *_a, **_k: True)
    seen: dict[str, list[tuple[str, int]]] = {}

    def fake_acquire(slots: list[tuple[str, int]]) -> Lease:
        seen["slots"] = slots
        return Lease((), "")

    monkeypatch.setattr(entry_guard, "_acquire", fake_acquire)
    lease = acquire_run("org-1", "user-1")
    assert lease is not None
    lease.release()
    assert seen["slots"] == [
        ("gate:running:global", 7),
        ("gate:running:org:org-1", 5),
        ("gate:running:user:org-1:user-1", 2),
    ]


def test_an_unlimited_dimension_stays_open_while_a_capped_one_fills():
    key = _key()
    lease = None
    try:
        lease = _acquire([(key, 1)])
        assert lease is not None
        assert _open(key, 0) is True
        assert _open(key, 1) is False
        lease.release()
        assert _open(key, 1) is True
    finally:
        _close([lease], key)
