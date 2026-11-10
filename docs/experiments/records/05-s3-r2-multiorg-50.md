# 实验：冒烟 S3 r2 — 多 org（带原始异常日志）

- **第几次**：S3 第 2 次（相对 [`04-s3-multiorg-50.md`](04-s3-multiorg-50.md)）
- **日期**：2026-11-09（UTC 2026-11-10T02:16Z 附近跑完）
- **代码**：本轮增加 `ProviderError.cause_type` / `cause`，`model.failed` 用 `log.error(..., exc_info=…)`；对照 `983f7ed` + 工作树
- **原始文件**：[`exports/load-test-smoke-s3-50-r2.json`](../exports/load-test-smoke-s3-50-r2.json)、[`exports/load-test-smoke-s3-50-r2.log`](../exports/load-test-smoke-s3-50-r2.log)

## 相对上一轮改了什么

上一轮账本只有 `PROVIDER_NETWORK`，worker 无 Traceback。本轮：

- `translate_provider_error` 写入 `cause_type` / `cause`
- `_fail` 打 ERROR + 完整栈，并写入 `model_calls.error_json`

模板、并发、租户分布与 S3 第 1 次相同（联采×3 + 海纳×3，50 条，concurrency 5）。

## 结果

| 指标 | S3 第 1 次 | S3 r2 |
|---|---|---|
| 请求 / 成功 / 超时 | 50 / 50 / 0 | **50 / 50 / 0** |
| 完成率 | 100% | **100%** |
| 成功 P50 | 6.16s | **3.70s** |
| 成功 P95 | 35.22s | **11.34s** |
| 成功 max | 43.46s | **11.53s** |

近 15 分钟 `model_calls`：DeepSeek 成功 140；Qwen 成功 5、失败 6。失败码仍是 `PROVIDER_NETWORK`，但 **cause 已可见**。

### 本轮 50 条会话的 Token 与花费（按 2026-11-09 官方价格，北京时间 10:15 高峰时段）

| Run 类型 | 模型 | 成功调用 | Prompt Tokens | Cache Hit（命中率） | Cache Miss | Completion Tokens | 实际花费 | 无缓存原价 |
|---|---|---|---|---|---|---|---|---|
| `chat` | `deepseek-flash`（高峰） | 95 | 115,305 | 98,560（85.5%） | 16,745 | 879 | ¥0.044464 | ¥0.237642 |
| `chat` | `qwen3.7-flash` | 5 | 12,280 | 6,528（53.2%） | 5,752 | 33 | ¥0.001438 | ¥0.002482 |
| `open` | `deepseek-flash`（高峰） | 50 | 18,950 | 6,400（33.8%） | 12,550 | 700 | ¥0.030956 | ¥0.043500 |
| **合计** | — | **150** | **146,535** | **111,488（76.1%）** | **35,047** | **1,612** | **¥0.076858（7.69 分）** | **¥0.283624（省 72.9%）** |

- **单请求总成本**：**¥0.001537 / 条**（`chat` ¥0.000918 + `open` ¥0.000619；因 Qwen async 流报错降级到高峰期 `deepseek-flash`，成本约为纯 Qwen 路径的 5.2 倍）。

## 原始异常（本轮抓到）

账本 `error_json`：

```text
code: PROVIDER_NETWORK
cause_type: TypeError
cause: 'async_generator' object is not iterable
```

worker 栈（节选，六次失败同构）：

```text
File "backend/llm/calls.py", _call_model_once → _invoke
File "backend/llm/calls.py", _invoke
  return _Transport(response=_assemble_stream(response, on_text), ...)
File "backend/llm/calls.py", _assemble_stream
  for chunk in chunks:
TypeError: 'async_generator' object is not iterable
```

HTTP 层仍常见 `200 OK` 后立刻 `model.failed`：请求到了 Qwen，同步代码却用 `for chunk in chunks` 去迭代 **async generator**，在本地炸成 `TypeError`，再被 `translate_provider_error` 的未知异常兜底标成 `network`。

因此：

1. **不是**并发打满 `GATE_*`，也不是典型的厂商断连。  
2. 上一轮 35–43s 长尾，很大概率是同一类失败 + outbox 退避；本轮失败更少/退避较短，P95 回到 ~11s。  
3. 根因在 `_assemble_stream` 未处理 async stream；应改为正确消费 async generator（或强制同步 stream），并把这类 `TypeError` 从「假装 network」里拆出来。

## 结论

1. 异常日志补丁有效：下次不必再猜 `PROVIDER_NETWORK`。  
2. S3 链通仍 100%；P95 改善来自本轮 Qwen 失败更少，不是租户分布变了。  
3. 下一步可修 `_assemble_stream` 的 async 迭代，再复测 S3；实验 A 仍按计划 50→500→2000。

## 已修复

- `_assemble_stream` 经 `_iter_stream_chunks` 消费 async generator（Qwen AsyncOpenAI stream）。
- `TypeError: 'async_generator' object is not iterable` 归为 `PROVIDER_BAD_RESPONSE`，不再假装 `PROVIDER_NETWORK`。
- 复测见 [`06-s3-r3-multiorg-50.md`](06-s3-r3-multiorg-50.md)。
