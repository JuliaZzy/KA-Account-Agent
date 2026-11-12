# 报错与重试

模型调用、Run 入队、HTTP 入口各有自己的“慢一点再试”。不要都叫 `RATE_LIMITED`。工具上游 429（`TOOL_RATE_LIMITED`）仍在 `run_loop` 里按工具注册表处理，不走这一套。

## 三种“限流”

| 发生在哪 | Python 类型 | `code` | 谁处理 |
|---|---|---|---|
| 创建 / 读取 Run 太频繁 | `AppError(429, …)` | 对外仍是 `RATE_LIMITED` | HTTP 入口，进不了 worker |
| 本地 `acquire_model()` 槽满 | `ModelCapacityExceeded` | `MODEL_CAPACITY_EXCEEDED` | worker，不扣业务次数 |
| 厂商 HTTP 429 / 5xx / 超时 / 断网 | `ProviderError` | `PROVIDER_RATE_LIMITED` 等 | worker + 熔断器 |

HTTP 对外码不改：前端已经认入口的 `RATE_LIMITED`。本地槽满和厂商 429 的监控含义不同，所以内部 `error_json.code` 分开。

`ProviderError.kind`：

| kind | 典型来源 | 可重试 | 进熔断器 |
|---|---|---|---|
| `rate_limited` | HTTP 429 | 是 | 是 |
| `server_error` | 500、502、503、504 | 是 | 是 |
| `timeout` | 连接 / 读超时 | 是 | 是 |
| `network` | 断连、DNS | 是 | 是 |
| `circuit_open` | 熔断器打开 | 是 | 否（避免自己把自己重新打开） |
| `authentication` | 401、403 | 否，Run 失败 | 否 |
| `bad_request` | 400、422 | 否 | 否 |
| `bad_response` | 200 但响应不是对象 | 否 | 否 |

模型输出 JSON 解析失败仍是 `ValueError`，不算厂商 HTTP 故障，走下面的业务重试（扣 attempt）。

## 只让一层重试

```
client adapter     识别 HTTP / SDK 异常 → ProviderError
                   OpenAI SDK max_retries=0；一次失败就抛，不 sleep
call_model         查熔断器 → acquire_model → 调厂商 → 记账
                   不 sleep、不重试
worker / Run       读 Retry-After，释放 model lease 和 run lease
                   Run 回 queued，spend_attempt=False
outbox dispatcher  到期的 outbox 交给 Celery
```

`execute_run` 遇到 Postgres 拒绝连接（`OperationalError`，如 `too many clients`）时，在任务内按 2–6 秒起、线性增长的 jitter 重试，最多 5 次，用完再抛，由 sweeper 补投兜底；否则消息被 ack 而 Run 停在 `queued`。

Celery 任务 `execute_run` 的 `max_retries=None` 用于队列关闭和各类 `SoftBusy`（`SessionBusy`、`GateFull`、`QueueDisabled`，以及 title 高峰软降级），不负责厂商错误或 worker 丢失。worker 丢失导致的 broker 重投由执行 outbox 在 PostgreSQL 中单独计数，超过上限后进入现有死信流程；偶发丢失仍会重新执行。厂商错误由 worker 写回 outbox，再投一次。

以前会叠在一起：SDK 重试 × 客户端循环 × worker 再试。现在客户端一次失败就抛，由 worker 统一等。

## Retry-After 和退避

`Retry-After` 是响应头，不是异常类型。可能是秒数：

```text
Retry-After: 12
```

也可能是 HTTP 日期。解析之后存成 `ProviderError.retry_after_seconds`。

worker 决定等待时间：

1. 有 `Retry-After`：用它，加 0.9–1.1 的扰动，上限 **300 秒**。
2. 没有这个头：full jitter 指数退避。厂商错误从 1 秒起（1、2、4、8、16…，封顶 60 秒）；本地槽满从 5 秒起。每次在 `[0, backoff]` 里随机取。

`schedule_retry(..., spend_attempt=False)` 把等待写在 outbox 的 `next_attempt_at` 上。

### queued Run 的安全补投

Celery 消息已经标成 `dispatched` 但 worker 没有把 Run 从 `queued` 领走时，sweeper 每 15 秒扫一轮，宽限期默认 120 秒（`KA_ACCOUNT_REDISPATCH_AFTER_SECONDS`）。宽限期取长是因为排队很深时 Run 只是在等，每补投一次就少一次机会；查询从 `agent_runs` 出发，先筛选 `status='queued'`、`queued_at` 已超过宽限期、存在老的 `dispatched execute_run`，再按 `queued_at` 限制 100 条；`idx_agent_runs_queued_at` 是只包含 queued 行的 PostgreSQL 部分索引。补投前用 `FOR UPDATE SKIP LOCKED` 锁住 Run，在同一事务中递增 `redispatch_count` 并刷新 `queued_at`，多个 sweeper 不会拿到同一条。

每次只领取一条并立即发布；发布抛错时，仅在 Run 仍处于同一次 queued 领取状态下回退次数，后续 Run 不消耗预算。发布结果不确定时仍可能重复投递，worker 的 queued→running 领取保证只有一个执行者。清理 outbox 时也在数据库里先排除仍活跃的 Run，再限制批量，避免旧的活跃行挡住后续可清理行。

**补投之前先看队列深度。** 聊天 Run 看 `ka_account`，标题（OPEN）Run 看 `ka_account.title`，各队列分别判断：Redis 里该队列还有消息，就说明 Run 只是在排队而不是丢了，不补投也不判耗尽；读不到 Redis 也跳过。否则一个积压的队列会被补出更多重复消息而更深（A3 r2：标题队列积压 p50 244 秒，补投事件 3083 次，24 个健康 Run 被误判 `STUCK_REDISPATCH_EXHAUSTED`）。真丢消息的 Run 由「超龄关闭」兜底。

**SoftBusy 重试要打心跳。** Celery 的 `self.retry` 把任务当作带 ETA 的消息放在 worker 内存里，此时 Redis 队列可以是空的，而 Run 其实在合法等待（会话里另一个 Run 还在跑、运行闸门没有空位、队列被关闭、标题队列高峰重试）。如果不留痕，sweeper 会把它当成丢了去补投，等久了还会按超龄关掉。所以每次重试之前 worker 把 `agent_runs.queue_heartbeat_at` 写成当前时间；补投和超龄都以「`queued_at` 与心跳较晚者」判断。心跳单独存一列，不动 `queued_at`：监控里「排队多久了」仍是真实的入队时间，过载时看得出来。写心跳失败不会阻止重试。

一条 Run 最多补投 3 次。仍没有 worker 领取时，sweeper 将它转为 `failed`，错误码为 `STUCK_REDISPATCH_EXHAUSTED`，同时写入死信，避免无限重复投递。排队超过 `KA_ACCOUNT_QUEUED_MAX_AGE_SECONDS`（默认 2 小时）仍没有任何动静的 Run 以 `STUCK_QUEUE_EXPIRED` 失败并进入死信，不补投、不重放：发消息的人早已不在等，重跑只会为没人读的回复付模型费。这一步不依赖 Redis，Redis 读不到时孤儿 Run 也照样被关闭。「有动静」取 `queued_at`（入队或补投时间）与 `queue_heartbeat_at`（worker 对 SoftBusy 重试的心跳）中较晚的一个。补投耗尽时状态机经过 `running` 再进入 `failed`。清理任务每轮最多删除 1000 条超过保留期、且对应 Run 已终态的 `dispatched` outbox；默认保留 7 天。仍 queued 的 outbox 不会被清掉，以免丢失补投依据。

## 重试顺序是不是 FIFO？

**不是按“谁先失败谁先跑”的严格 FIFO。** 到期之后按 `next_attempt_at` 从早到晚取，同一时刻到期的，谁的时间戳更早谁先投。

具体是两段：

1. **Outbox。** `find_pending_outbox_jobs` 只拿 `status=pending` 且 `next_attempt_at <= now` 的行，`ORDER BY next_attempt_at`。第一次入队时 `next_attempt_at` 约等于创建时间，看起来像 FIFO。被 429 打回来、要等 12 秒的那条，会排到已经到期的其它 Run 后面。
2. **Celery。** dispatcher 把到期的 outbox 推进 broker。broker 里大致是投递顺序。Celery 的并发 worker 领到之后就可以跑，不保证和 outbox 扫描顺序逐条对应。

所以：没有 Retry-After、同时到期的 Run，接近 FIFO；有人带着更长的等待回来，会让路给已经到期的。这是有意的——厂商说 12 秒后再来，就不要插到队头去抢那 10 次/分钟的配额。

## `spend_attempt=False` 不会死信

业务重试（普通 `Exception`、模型输出解析失败）走 outbox 的执行次数，默认 3 次，超过进入 `dead_letter_jobs`，Run 失败。间隔现在是 1 分钟、5 分钟、15 分钟。对话场景用户还在等 SSE，5 分钟这一档过长；短重试应是 5 秒、30 秒，长退避只留给没有人盯着的后台。厂商 429 不走这三档，见上一节。

基础设施重试（本地槽满、厂商可重试错误、数据库 `OperationalError`）**不增加** outbox `attempts`，因此 **不会因为重试次数进死信**。现在的收场条件是 Run 的 `max_duration_seconds`：领到时已经超时，或 sweeper 扫到超时，记 `failed` / `COST_BUDGET_EXCEEDED`。

高峰时一条 Run 可能要排队等好几轮 429。如果按次数死信，会在厂商恢复前把对话判死。次数上限以后再说；现在靠时长封顶。

## 熔断器

按 **brand**（`qwen` / `deepseek` / `glm`）一把 Redis 钥匙，不按模型名。只统计厂商真正的错误：429、5xx、timeout、network。本地 `acquire_model()` 槽满、401、400、解析失败都不算。

默认：**连续 5 次** 上述错误 → 打开 **30 秒**。冷却期间 `call_model` 不发请求，抛 `ProviderError(kind=circuit_open)`，worker 按基础设施重试。冷却结束允许 **一次** half-open 探测；成功则关闭，失败再打开 30 秒。

环境变量：

```text
KA_ACCOUNT_CIRCUIT_FAILURES=5          # 0 表示关掉熔断
KA_ACCOUNT_CIRCUIT_COOLDOWN_SECONDS=30
```

### 5 次 / 30 秒是什么意思，怎么改？

- **阈值 5**：同一 provider 连续失败 5 次才停手。太小（2）时一次抖动就全员排队；太大（50）时会在明确挂掉的厂商上继续打。如果日常就是配额不够而不是厂商宕机，把阈值调到 10–20，或先看监控里 `PROVIDER_RATE_LIMITED` 是不是成片出现、有没有伴随 5xx。
- **冷却 30 秒**：打开之后至少这么久不发请求。太短等于还在锤；太长则厂商已经好了还在空等。若 `Retry-After` 经常是 6–12 秒，30 秒冷却偏保守，可以改成 15。如果打开之后探测仍然 503，保持 30 或加到 60。
- **怎么判断该改**：看 `model_calls.error_json` 的 `kind` 和 `retry_after_seconds`，以及 outbox 的 `infrastructure_attempts`。几乎都是 429 且带 Retry-After → 调高阈值、缩短冷却，让 Retry-After 当主节奏。出现连续 502/超时 → 阈值可以低一点，冷却可以长一点。

熔断器坏了（Redis 报错）时放行，避免闸门把所有模型调用挡住。

## 流式回答和 generation_id

打字机还在。每次 `call_model` 带 `on_text` 时发一个 `generation_id`，写进 running 的 `output_json`：

```json
{ "text": "Hel", "generation_id": "…" }
```

前端 `followRun` 看到新的 `generation_id` 就丢掉上一轮 live token，从新文本开始画。半截 503 之后 worker 会先写一轮空文本和新 id，再把 Run 放回 queued，避免失败那一段一直挂在屏幕上。正文增量同时通过 `run.reply_updated` 事件推送，成功提交时 `complete_run` 仍只写最终 `{ "text": "…" }`。

不要在客户端里把第二次请求的 token 接到第一次后面。重试是整段 Run 再领一次，新的 generation。

## 代码在哪

| 文件 | 职责 |
|---|---|
| `backend/llm/errors.py` | `ProviderError`、`Retry-After` 解析、SDK/HTTP 映射 |
| `backend/llm/client.py` | 适配层；`max_retries=0` |
| `backend/llm/calls.py` | `call_model`：熔断 → 占座 → 调用；流式带 generation_id |
| `backend/llm/breaker.py` | Redis 熔断器 |
| `backend/services/run_worker.py` | 基础设施重试 / 不可重试则失败 |
| `backend/db/database/queue.py` | `schedule_retry`；`delay` 覆盖计算出的等待 |
| `frontend/src/chat-live.ts` | `liveFromRun`：generation_id 变了就清空 live |

采购、合同、报表、查数、子任务里对 `ModelCapacityExceeded` 的放行同样适用于 `ProviderError`，避免厂商 503 被收成工具 `ok: false`。
