# 记录 36：实验 E 盘点 — 已有单测 vs 缺口

E 要覆盖的故障：厂商 429、调用超时、worker 中断、lease 过期、stale outbox、`response_lost`。副作用不得自动重放。这一步只对照代码和测试，不跑 live、不加 worker、不改闸门。

## 已经有单测的

| 故障 | 测试 | 产品行为 |
|---|---|---|
| HTTP 429 + `Retry-After` | `tests/test_provider_errors.py::test_http_429_maps_retry_after`、`test_translate_maps_status_codes` | `PROVIDER_RATE_LIMITED`，可重试 |
| 429 进队列 | `tests/test_run_api.py::test_provider_rate_limit_requeues_with_retry_after` | 不花业务 attempt，按 Retry-After 再投 |
| 超时异常可重试 | `test_timeout_exception_is_retryable` | `TimeoutError` → `PROVIDER_TIMEOUT` |
| 401 不可重试 | `test_http_401_is_not_retryable`、`test_provider_authentication_fails_the_run` | 直接失败 |
| 熔断 | `test_circuit_opens_after_consecutive_vendor_failures_and_allows_one_probe` | 连续 429/5xx/timeout/network |
| Qwen 30s / 静默 10s | `tests/test_llm.py` 三条 Qwen timeout | `asyncio.wait_for`；SDK 仍是 7200s |
| worker 丢失不重发副作用 | `tests/test_execution_contract.py::test_a_lost_worker_resumes_without_sending_twice` | 已 claim 的邮件不发第二次 |
| lease 过期清扫 | `test_an_expired_lease_is_swept_without_spending_a_retry`、`test_three_expired_leases_fail_the_run` | 不花重试次数；三次后失败 |
| stale outbox / 补投 | `test_an_outbox_past_max_attempts_is_dead_lettered`、`tests/test_unclaimed_dispatch.py` | 死信；队列非空不误杀 |
| 副作用超时 → `response_lost` | `test_a_side_effect_timeout_is_response_lost` | 不自动再调；第二次看到 `skipped_duplicate` |
| 计划中的副作用丢响应 | `test_planned_side_effect_with_lost_response_is_not_retried` | 不重试 |
| 仍在 running 的副作用 | `test_a_left_running_side_effect_is_not_called_again` | 不重入 |
| 过期副作用 | `test_a_stale_side_effect_becomes_response_lost` | 标丢失，等人 |
| 采购确认幂等 | `tests/test_submit.py::test_confirm_creates_one_order_and_burns_the_token`、`test_double_click_races_produce_exactly_one_order` | token 在建单前烧掉 |
| SoftBusy / DB 拒绝 | `tests/test_execute_run_db_retry.py` | 心跳 + 抖动重试 |

采购单的副作用不走 tool `response_lost`，走 confirm token。这条已经覆盖，不必再做一个「杀 worker 再下单」的 live。

## 缺口

1. **DeepSeek（以及同步的 GLM chat）单次调用没有有效超时。** `LLMSettings.timeout=7200`。Qwen 有 `_call_qwen` 的 `wait_for`；`_call_deepseek` / `_call_glm` 没有。记录 14 留下一次 30.2s 的 DeepSeek 采购抽取。`asyncio.wait_for` 打不穿同步 OpenAI SDK，所以只加一层协程超时不够，HTTP timeout 才是天花板。
2. **厂商超时进队列**没有单独用例（429 有）。路径与 429 相同（`ProviderError.retryable`），缺的是断言 `PROVIDER_TIMEOUT` 也不花业务 attempt。
3. **Live 429 / 真厂商超时**没有注入点。实验室用 FakeLLM / 抛 `ProviderError`，不会打到真实 DeepSeek 的 429。短超时小样本可以在补上 DeepSeek 上限之后用单测注入，不必先烧一次真实月报。

不把记录 35 的 `plan-02` 300s 客户端超时算进 E：那是 Qwen 审阅变慢，不是恢复路径。

## 下一步

先跑现有实验室故障单测确认没漂；再补 DeepSeek/GLM 的 30s 上限和超时进队列用例。
