# 记录 38：实验 E — DeepSeek 超时上限

记录 14 留下一次 30.2s 的 DeepSeek 采购抽取：当时只有 Qwen 有 30s 上限，DeepSeek 的 SDK timeout 是 7200s。记录 36 把它标成 E 的产品缺口。

## 改动

- DeepSeek / GLM 的 OpenAI SDK HTTP timeout 从 7200s 改成 30s（`KA_ACCOUNT_DEEPSEEK_TIMEOUT_SECONDS` / `KA_ACCOUNT_GLM_TIMEOUT_SECONDS`）。同步 SDK 上 `asyncio.wait_for` 打不穿阻塞调用，HTTP timeout 才是真天花板。
- `_call_deepseek` 再加一层总时长 30s、chunk 静默 10s（与 Qwen 同口径）。阻塞调用放到 worker 线程里，这样单测可以用短超时注入，不必打真实厂商。
- Qwen 仍走原来的 `wait_for`，SDK 保持 7200s，不改这条已经测过的路径。
- 厂商 `timeout` 进队列：不花业务 attempt，与 429 同一条 `ProviderError.retryable` 路径。

OCR 的 `GLMOCRClient` 仍是 180s，没动。

## 测试（注入，不是 live 厂商）

- 流静默 → `TimeoutError` → `PROVIDER_TIMEOUT`，可重试
- 总时长天花板：chunk 一直来也在 0.2s 切掉
- 非流式阻塞调用（B1 那类 JSON 抽取）在 0.2s 切掉
- 两个上限之内的短流能跑完
- SDK：DeepSeek / GLM = 30s，Qwen = 7200s
- `test_provider_timeout_requeues_without_spending_an_attempt`：`PROVIDER_TIMEOUT` 把 Run 放回 `queued`，`attempts` 仍为 0

没有对真实 DeepSeek 打 429，也没有用 0.2s 去烧一条真实月报。实验室注入已经覆盖分类、降级入口和进队列；live 429 需要厂商或代理配合，不是这一步的入口条件。

## 预期影响

正常 DeepSeek 调用（月报 2–5s，洞察最常见 7–18s）远低于 30s，不该被误杀。被切掉的调用记 `PROVIDER_TIMEOUT`，可重试并走路由降级，而不是挂到 7200s。
