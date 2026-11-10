# 实验：冒烟 S3 r3 — 多 org（修 async stream）

- **第几次**：S3 第 3 次（相对 [`05-s3-r2-multiorg-50.md`](05-s3-r2-multiorg-50.md)）
- **日期**：2026-11-09（UTC 2026-11-10T02:25Z 附近跑完）
- **代码**：`_call_qwen` 在同一次事件循环里缓冲 async stream；`_await` / `_iter_stream_chunks` 共用进程内持久 loop；`async_generator` / `Event loop is closed` 归为 `PROVIDER_BAD_RESPONSE`
- **原始文件**：[`exports/load-test-smoke-s3-50-r3.json`](../exports/load-test-smoke-s3-50-r3.json)、[`exports/load-test-smoke-s3-50-r3.log`](../exports/load-test-smoke-s3-50-r3.log)

## 相对上一轮改了什么

r2 确认根因是 `_assemble_stream` 对 Qwen `AsyncOpenAI` 返回的 async generator 做了同步 `for`。本轮：

1. `_call_qwen` 在 create 的同一协程里把 async stream 拉成 list，再交给 `_assemble_stream`。
2. 进程内持久 event loop，避免 `asyncio.run` 关掉 loop 后出现 `Event loop is closed`。
3. 本地 stream / loop 类 `TypeError` / `RuntimeError` 不再假装 `PROVIDER_NETWORK`。

模板、并发、租户分布与 S3 第 1 / r2 相同。

中间有一次未完成修复的复测（`Event loop is closed`）和一次 worker 重启撞上的 `APIConnectionError` 长尾，产物分别留在 `exports/load-test-smoke-s3-50-r3-eventloop.*`、`exports/load-test-smoke-s3-50-r3-restart-blip.*`，不计入本表。

## 结果

| 指标 | S3 第 1 次 | S3 r2 | **S3 r3** |
|---|---|---|---|
| 请求 / 成功 / 超时 | 50 / 50 / 0 | 50 / 50 / 0 | **50 / 50 / 0** |
| 完成率 | 100% | 100% | **100%** |
| 成功 P50 | 6.16s | 3.70s | **4.75s** |
| 成功 P95 | 35.22s | 11.34s | **8.79s** |
| 成功 max | 43.46s | 11.53s | **9.81s** |

本轮 `model_calls`（按压测时间窗筛）：Qwen 成功 253、失败 0；DeepSeek 成功 2。无 `async_generator` / `Event loop is closed`。

### 本轮 50 条会话的 Token 与花费（对比 S3 r2 修复前）

| Run 类型 | 模型 | 调用数 | Prompt Tokens | Cache Hit（命中率） | Cache Miss | Completion Tokens | 实际花费 | 无缓存原价 |
|---|---|---|---|---|---|---|---|---|
| `chat`（50 条） | `qwen3.7-flash` | 100 | 136,732 | 108,800（**79.6%**） | 27,932 | 558 | **¥0.010385** | ¥0.027793 |
| `open`（50 条） | `qwen3.7-flash` | 50 | 20,241 | 2,560（**12.6%**） | 17,681 | 904 | **¥0.004362** | ¥0.004771 |
| **S3 r3 合计** | `qwen3.7-flash` | **150** | **156,973** | **111,360（70.9%）** | **45,613** | **1,462** | **¥0.014747（1.47 分）** | **¥0.032564（省 54.7%）** |

- **单请求总成本**：**¥0.000295 / 条**（`chat` ¥0.000208 + `open` ¥0.000087）。
- **对比 S3 r2（修复前大量 fallback 到高峰期 `deepseek-flash`）**：50 条总花费从 **¥0.076858（7.69 分）** 降至 **¥0.014747（1.47 分）**，**直接降低 80.8%**。

## 结论

1. Qwen 流式组装已通；问候路径基本走 Qwen，不再被本地 TypeError 打成「网络失败 + 退避」。
2. P95 从 35s → 11s → **8.8s**，与 r2「其实已经好很多」的方向一致，本轮去掉了剩余本地失败。
3. 实验 A 仍按计划 50→500→2000。

## 经历怎么理解

一句话：Qwen async 代码写错 → 重试/降级把 P95 拉长 → 按 async 修，但 `asyncio.run` 拆两次又炸 → 最终在同步 worker 边界上把整段 async 一次跑完 → 成功。

两层别混：

- **厂商 SDK**：Qwen 是 `AsyncOpenAI` → 发请求、收 stream 都是 **async**
- **我们的 worker**：Celery 任务是普通同步函数 → 不能直接 `async for`，必须在某个地方把 async「跑完」变成普通返回值

最后一步**不是**改成同步 HTTP 客户端；客户端仍是 async，只是在 Celery 同步边界上正确接住它。

## 「asyncio.run 关掉 loop 后再 drain」是什么意思

错的写法本质是 **拆成两次跑**：

```text
第 1 次 asyncio.run：
  await chat_completion(stream=True)
  → 得到 async_generator（它绑在「这次用的那个 loop」上）
  → asyncio.run 结束时把这个 loop 关掉

第 2 次 asyncio.run：
  async for chunk in 那个 generator   ← drain（把流拉完）
  → 但 generator 还挂在已关掉的 loop 上
  → RuntimeError: Event loop is closed
```

`drain` = 把流式 chunk 一个个拉完，攒成完整结果。  
异步流不能跨「已关闭的 loop」继续用，所以第二次再 drain 就炸。

## 最终形态具体流程

```text
Celery 同步任务
  └─ _call_qwen()                    ← 对外仍是同步函数
       └─ _await(_run())             ← 用进程里同一个长期 loop 跑一次
            └─ async def _run():
                 1. await chat_completion(stream=True)   ← 还是 async 网络 IO
                 2. [chunk async for chunk in resp]      ← 同一次里把流拉完
                 3. return list[chunk]                   ← 已经是普通 list
  └─ _assemble_stream(list, ...)     ← 纯同步 for，拼文本
```

要点：**create 和 drain 在同一次 `_await` / 同一个 loop 里做完**，出来时已经是普通 `list`，后面再同步组装就没问题。

## 「又同步又异步」怎么理解

| 层 | 同步/异步 | 谁 |
|---|---|---|
| 网络/SDK | 异步 | `AsyncOpenAI`、`await`、`async for` |
| 任务边界 | 同步 | Celery worker、`_call_qwen` 返回值、`_assemble_stream` |

不是改成了同步 HTTP 客户端；是 **在同步边界上，把整段 async 工作一次性跑完再交出去**。  
DeepSeek 那路本来就是同步 `OpenAI`，所以不需要这套；Qwen 才需要。
