"""Official vendor token pricing in CNY (RMB) for supported models.

Prices are per 1,000,000 tokens and follow the vendor billing pages checked on
2026-11-09:

- Alibaba Cloud Model Studio (Bailian, Beijing region) ``qwen3.7-flash``:
  - ``0 < prompt_tokens <= 32_000``: input ¥0.20 / 1M, output ¥0.80 / 1M
  - ``32_000 < prompt_tokens <= 256_000``: input ¥0.60 / 1M, output ¥2.40 / 1M
  - ``prompt_tokens > 256_000``: input ¥1.20 / 1M, output ¥4.80 / 1M
  - Implicit context cache hit: 20% of standard input price (¥0.04 / 1M on the
    <=32K tier). Explicit cache creation: 125% of standard input price; explicit
    cache hit: 10% of standard input price.
- DeepSeek official API ``deepseek-flash`` (``DeepSeek-V4.1-Flash-1017``):
  - Peak hours (Monday-Friday 09:00-12:00 and 14:00-18:00 Beijing time, UTC+8):
    cache hit input ¥0.04 / 1M, cache miss input ¥2.00 / 1M, output ¥8.00 / 1M.
  - Off-peak hours (all other weekday hours and all day Saturday/Sunday):
    cache hit input ¥0.02 / 1M, cache miss input ¥1.00 / 1M, output ¥4.00 / 1M.
- Zhipu GLM-OCR: input and output ¥0.20 / 1M tokens, checked 2026-11-10.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

_BEIJING_TZ = timezone(timedelta(hours=8))
_PER_MILLION = 1_000_000.0


def is_deepseek_peak(at: datetime | None = None) -> bool:
    """Return True when ``at`` falls in DeepSeek's weekday Beijing peak windows.

    When ``at`` is omitted, default to peak pricing so budget guards do not
    under-estimate daytime traffic.
    """
    if at is None:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    bj = at.astimezone(_BEIJING_TZ)
    if bj.weekday() >= 5:
        return False
    hm = (bj.hour, bj.minute)
    return (9, 0) <= hm < (12, 0) or (14, 0) <= hm < (18, 0)


def _qwen_tier_rates(prompt_tokens: int) -> tuple[float, float]:
    """Return ``(input_cny_per_1m, output_cny_per_1m)`` for ``qwen3.7-flash``."""
    if prompt_tokens <= 32_000:
        return 0.20, 0.80
    if prompt_tokens <= 256_000:
        return 0.60, 2.40
    return 1.20, 4.80


def _split_prompt_tokens(
    prompt_tokens: int,
    cache_hit_tokens: int | None,
    cache_miss_tokens: int | None,
    cache_creation_tokens: int | None,
) -> tuple[int, int, int]:
    """Return ``(hit, miss, creation)`` nonnegative token counts."""
    prompt = max(0, int(prompt_tokens or 0))
    hit = max(0, int(cache_hit_tokens or 0))
    creation = max(0, int(cache_creation_tokens or 0))
    if cache_miss_tokens is None:
        miss = max(0, prompt - hit - creation)
    else:
        miss = max(0, int(cache_miss_tokens or 0))
        if hit == 0 and miss == 0 and creation == 0 and prompt > 0:
            miss = prompt
    return hit, miss, creation


def call_cost_cny(
    provider: str,
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_hit_tokens: int | None = None,
    cache_miss_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    created_at: datetime | None = None,
) -> float:
    """Return the exact estimated CNY cost of one model call."""
    brand = (provider or "").strip().lower()
    name = (model or "").strip().lower()
    if brand == "fake":
        return 0.0
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    if name == "glm-ocr":
        return (prompt + completion) * 0.20 / _PER_MILLION
    hit, miss, creation = _split_prompt_tokens(
        prompt, cache_hit_tokens, cache_miss_tokens, cache_creation_tokens,
    )
    if name == "qwen3.7-flash" or brand == "qwen":
        in_rate, out_rate = _qwen_tier_rates(prompt)
        hit_rate = in_rate * (0.10 if creation > 0 else 0.20)
        create_rate = in_rate * 1.25
        return (
            miss * in_rate
            + hit * hit_rate
            + creation * create_rate
            + completion * out_rate
        ) / _PER_MILLION
    if name == "deepseek-flash" or brand in ("deepseek", "ds"):
        peak = is_deepseek_peak(created_at)
        in_rate = 2.00 if peak else 1.00
        hit_rate = 0.04 if peak else 0.02
        out_rate = 8.00 if peak else 4.00
        return ((miss + creation) * in_rate + hit * hit_rate + completion * out_rate) / _PER_MILLION
    return 0.0


def call_uncached_cost_cny(
    provider: str,
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    created_at: datetime | None = None,
) -> float:
    """Return what the call would cost in CNY if all input tokens were cache misses."""
    return call_cost_cny(
        provider,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=0,
        cache_miss_tokens=prompt_tokens,
        cache_creation_tokens=0,
        created_at=created_at,
    )


def call_cost_cents(
    provider: str,
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_hit_tokens: int | None = None,
    cache_miss_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    created_at: datetime | None = None,
) -> float:
    """Return the exact estimated cost in CNY cents (分, 1 CNY = 100 cents)."""
    return call_cost_cny(
        provider,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        cache_creation_tokens=cache_creation_tokens,
        created_at=created_at,
    ) * 100.0


def estimate_cost_cents(
    provider: str,
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_hit_tokens: int | None = None,
    cache_miss_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    created_at: datetime | None = None,
) -> int:
    """Return the integer-cent column value for ``usage_records.estimated_cost_cents``."""
    return round(call_cost_cents(
        provider,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        cache_creation_tokens=cache_creation_tokens,
        created_at=created_at,
    ))


def _row_field(row: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def sum_usage_cost_cents(rows: Iterable[Mapping[str, Any] | Any]) -> int:
    """Sum usage rows in fractional cents before rounding to an integer cent total.

    A single short call on ``qwen3.7-flash`` costs ~0.02 cents and rounds to 0 on
    its own row, while 500 calls cost ~15 cents. Summing the exact per-row cost
    first preserves sub-cent usage across a run or day.
    """
    total = 0.0
    for row in rows:
        stored = int(_row_field(row, "estimated_cost_cents", 0) or 0)
        exact = call_cost_cents(
            str(_row_field(row, "provider", "") or ""),
            str(_row_field(row, "model", "") or ""),
            prompt_tokens=int(_row_field(row, "prompt_tokens", 0) or 0),
            completion_tokens=int(_row_field(row, "completion_tokens", 0) or 0),
            cache_hit_tokens=_row_field(row, "cache_hit_tokens"),
            cache_miss_tokens=_row_field(row, "cache_miss_tokens"),
            cache_creation_tokens=_row_field(row, "cache_creation_tokens"),
            created_at=_row_field(row, "created_at"),
        )
        total += max(float(stored), exact)
    return round(total)
