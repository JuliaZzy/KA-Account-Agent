# 实验：A3 — 运行链路基线（问候，单 org 多用户，2000 条）

- **第几次**：实验 A 第 3 档（50 → 500 → **2000**；相对 [`A2`](09-a2-greeting-500.md) 只改条数）
- **日期**：2026-11-09（UTC 2026-11-10T03:33Z–04:08Z，约 35 分钟）
- **代码**：与 A2 相同，未改代码
- **原始文件**：[`exports/load-test-greeting-a3-2000.json`](../exports/load-test-greeting-a3-2000.json)、[`exports/load-test-greeting-a3-2000.log`](../exports/load-test-greeting-a3-2000.log)

## 目的

同模板、并发、timeout、租户分布，把样本量放到 2000，把 P99 报稳，并观察长时间稳态下有没有漂移、失败、残留 queued、成本偏离。

真实模型压测，合成问候模板，不需要真实质量测试集。

## 配置

| 项 | 值 |
|---|---|
| 命令 | `./dev py scripts/load_test_chat.py --base-url http://api:8000 --partnership-id ps-liancai-01 --user-id user-liancai-chen --user-ids user-liancai-chen,user-liancai-lin,user-liancai-zhou --requests 2000 --concurrency 5 --output docs/experiments/exports/load-test-greeting-a3-2000.json` |
| 消息模板 | `你好，这是第 {i} 条合成压测消息，请回复一句确认。` |
| 租户 | `ps-liancai-01` × 3 用户轮转（chen 667 / lin 667 / zhou 666） |
| Worker / title-worker | 5 / 2 |
| `GATE_USER` | 5 |
| HTTP / completion timeout | 30s / 180s |
| 跑前状态 | worker / title-worker / api / sweeper 均 healthy；worker 已预热（S4 r3 之后一直在跑） |

## 结果

| 指标 | A2（500） | **A3（2000）** |
|---|---|---|
| 请求 / 成功 / 超时 | 500 / 500 / 0 | **2000 / 1998 / 2** |
| 完成率 | 100% | **99.9%** |
| 成功 P50 | 4.91s | **4.62s** |
| 成功 P95 | 6.46s | **6.02s** |
| 成功 P99 | 7.18s | **7.38s** |
| 成功 max | 8.23s | **73.67s** |
| 含超时的观测 max | 8.23s | 180.25s |
| 吞吐（整轮） | ≈ 0.93 条/s | ≈ 2000 / 2114s ≈ **0.95 条/s** |

分 400 条一块看有没有漂移（成功样本）：

| 区间 | P50 | P95 | max |
|---|---|---|---|
| 1–400 | 4.60s | 5.80s | 7.63s |
| 401–800 | 4.67s | 6.11s | 73.67s |
| 801–1200 | 4.61s | 6.14s | 33.99s |
| 1201–1600 | 4.62s | 5.91s | 7.33s |
| 1601–1998 | 4.58s | 6.13s | 8.56s |

P50 全程在 4.58–4.67s，没有漂移。P95 / P99 在稳态下与 A2 一致，**主体是健康的**。

不健康的是尾部：2 条超时、8 条 >10s（最慢 73.7s，另有 34.0s 和若干 13–20s）。

## 调查

账本（`model_calls` / `agent_runs` / `outbox_jobs` / `run_events`，按压测时间窗 03:33–04:10 UTC）：

| 项 | 值 |
|---|---|
| `agent_runs` chat | succeeded **1998**、**queued 2** |
| `agent_runs` open | succeeded 1998 |
| `outbox_jobs` | `dispatched` 3998，无 dead letter |
| Qwen `model_calls` | 失败 2（均为 `APIConnectionError`，03:44 和 03:49 各 1 次） |
| DeepSeek | 2 次（上述 Qwen 失败后 fallback） |

### 问题 1：2 条超时 = Run 永远停在 `queued`（可靠性缺口）

超时的是第 533、534 条，对应的两个 Run 同时创建于 03:43:25.98 和 03:43:26.27：

- outbox 状态是 `dispatched`（Celery 消息已发出），`attempts=0`，**0 次模型调用，没有 `run.started`**，worker 从未真正开始执行；
- 同一时刻 worker 日志里这两个 `execute_run` 任务都抛了 `psycopg.OperationalError: FATAL: sorry, too many clients already`——**Postgres 连接数在那一秒触顶**（`max_connections` = 100）；
- Postgres 日志里整个时间窗只有这两次连接被拒，即这是一次约 1 秒的瞬时尖峰，不是持续耗尽；API / title-worker 没有同类错误；
- 约 10 分钟后（03:53:31）sweeper 给这两个 Run 打了 `run.queue_stale`（`queue_unclaimed`），到现在仍是 `queued`。

这里原先的判断是「sweeper 只标记不补投」，**是错的**。sweeper 每 15 秒一轮，里面本来就有补投：`republish_unclaimed`（outbox 已 `dispatched` 超过 30 秒、Run 仍是 `queued` 就重发 `execute_run`）。`run.queue_stale` 只是 10 分钟后额外打的观测标记，两者并存。补投没起作用，是因为扫描有 bug（见下）。

**补投为什么没命中这两条（已用只读脚本验证）：**

- `unclaimed_dispatches` 先 `ORDER BY dispatch_at LIMIT 100` 取「最老的 100 条 `dispatched` 的 execute_run outbox」，**之后**才在 Python 里过滤「对应 Run 仍是 queued」。LIMIT 在过滤之前。
- `outbox_jobs` 里 `dispatched` 的行从不清理：现在有 **6,208** 条超过 30 秒的，最老的是 11 月 2 日的。最老的 100 条对应的 Run 早就成功了，过滤后是空的，扫描永远走不到新的行。
- 这两个卡住的 Run 在这个排序里排第 **3271、3273** 位，远在 100 之外。
- 验证：`unclaimed_dispatches(limit=100)` 返回 **空**；`limit=100000` 返回 **71 个** queued Run，其中包含这两个。

所以：`execute_run` 任务遇到瞬时的数据库连接失败时没有重试，消息已经算「已投递」；而兜底的补投因为扫描 bug 永远看不到新的卡住的 Run。这和 S1 第 1 次「outbox dispatched + run 一直 queued」是同一类症状，只是这次的触发原因是连接数。该 bug 随 `outbox_jobs` 越积越多才会暴露（单测里行很少，所以测试通过）。

另外库里现在共有 **71 个 `queued` 的 Run**，除这两个外都是更早实验和开发遗留的。修补投扫描之前要先决定怎么处理它们：一旦补投能看到，它们会被重新执行并真的调用模型。

**谁占满了连接，本实验没有证据。** 现在静止状态下已有 47 个连接（41 个 idle），引擎配置是每进程 `pool_size 10 + max_overflow 20`，worker 5 进程 + title-worker 2 进程 + api + sweeper，理论峰值远超 100；但那一秒具体是哪些进程、或者是否有别的窗口在同时连数据库（比如跑测试），没有记录，不下结论。

### 问题 2：Qwen 单次调用偶发长尾（含一次 71 秒），且没有单次超时

- 慢 Run 之一（73.4s）：PLAN 调用 2.2s 正常，ANSWER 调用 `latency_ms` = **71,190**，成功返回；
- 03:49 前后一段时间 Qwen 调用集体变慢：7 次几乎一致的约 10.3s、1 次 29.8s 后 `APIConnectionError`、1 次 18.0s；对应 03:49:17–03:49:36 创建的几个 Run 各约 13s；
- 这些都是 Qwen 调用自己慢或连接失败，不是排队（这些 Run 的 `queue_s` 全是 0.0）。已确认压测期间**没有动过 VPN 和网络**，所以不能再归因到本机网络切换（A1 第 1 次那次才是）；更可能在 Qwen 供应商侧或出站路径上，本实验没有分段计时，无法进一步区分；
- 当前没有单次调用的总超时，一次卡住的流可以拖到 70 秒以上才返回。

### 问题 3：流式回复被缓冲，用户看不到逐字输出（上一轮修复带来的副作用）

71s 那个 Run 的事件显示：`run.reply_updated`（逐字回复）的 8 条事件全部发生在 03:47:53.825–03:47:53.841，间隔只有几毫秒，也就是**流结束后才一次性吐出**。原因是为了修 `Event loop is closed`，S3 r3 把 Qwen 的 async stream 先整段拉成 list 再组装，`on_text` 回调是在拉完之后才触发的。结果是：

- 延迟指标不受影响（最终耗时一样）；
- 但用户侧的流式体验失效：碰到上面 71s 这种慢调用时，用户 70 秒内什么都看不到，然后整句瞬间出现。

这是我们自己引入的行为变化，不是供应商问题。

## 成本

| 指标 | 值 |
|---|---|
| 总花费 | **¥0.5741**（无缓存原价 ¥1.2770，缓存省 **55.05%**） |
| 单请求 | **¥0.000287**（`chat` ¥0.000212 + `open` ¥0.000075） |
| Token | prompt 6,157,007 / completion 55,669（缓存命中 4,393,472，**71.4%**） |
| 模型调用 | 共 5,686 次：Qwen chat 3,994、Qwen open 1,690、DeepSeek 2 |
| 与预估 | A2 推算 ¥0.61，实测 ¥0.574，**低 6%** |

分项（按 `qwen3.7-flash` / `deepseek-flash` 官方价）：

| 项 | 调用 | 花费 | 无缓存原价 |
|---|---|---|---|
| Qwen `chat` | 3,994 | ¥0.422248 | ¥1.116061 |
| Qwen `open` | 1,690 | ¥0.150986 | ¥0.160416 |
| DeepSeek `chat`（fallback，2 次） | 2 | ¥0.001216 | ¥0.001216 |

- 2000 条总花费不到 6 毛钱，与 A2 的单条成本（¥0.000303）一致，成本在 4 倍样本下没有偏离。
- `open` 的模型调用 1,690 次少于 1,998 个 open Run，是因为一部分 open Run 走了「负载下放弃精修，保留临时标题」，不调模型，这是预期行为。
- 2 次 DeepSeek 都是 Qwen `APIConnectionError` 后的 fallback，共 ¥0.0012，可忽略。

## 结论

1. **稳态基线可用**：2000 条里 1998 条成功，P50 4.62s / P95 6.02s / P99 7.38s，分块无漂移，吞吐约 0.95 条/s，与 A2 一致；成功样本远超 300，P99 可报稳。
2. **但 A3 不是干净的 100%**：完成率 99.9%，原因是一次瞬时的数据库连接尖峰让 2 个 `execute_run` 任务失败，Run 永远 `queued`，而 sweeper 的补投因扫描 LIMIT 在过滤之前而看不到它们。这是需要修的可靠性缺口（问题 1），**不是性能问题，也不能靠加 worker 解决**（加 worker 会让连接更紧张）。
3. 尾部长尾（73s 等）来自 Qwen 单次调用慢 / 连接失败，缺少单次超时与更快的 fallback（问题 2）。
4. 流式输出被缓冲（问题 3）是上一轮修复引入的副作用，需要改成在 async 循环里边收边触发 `on_text`。
5. 成本：2000 条 ¥0.574，缓存省 55%，与预估一致。

## 修复与落地状态

三个代码问题已修，并有回归测试；**尚未用真实压测复验**（见「下一步」）。

| 优先级 | 问题 | 处理 | 状态 |
|---|---|---|---|
| 高 | 补投扫描 bug：`unclaimed_dispatches` 的 LIMIT 在「Run 仍 queued」过滤之前，被已完成的旧 outbox 行占满 | 以 `agent_runs` 为主表，数据库内先按 `status='queued'`、`queued_at`、补投次数过滤再 `LIMIT`；`idx_agent_runs_queued_at` 是只含 queued 行的部分索引；`FOR UPDATE SKIP LOCKED` 锁住后在同一事务里 `redispatch_count+1` 并刷新 `queued_at`，多个 sweeper 不会重复拿同一条 | **已修** |
| 高 | 补投没有上限，也分不清「在排队」和「丢了」 | 宽限期默认 120 秒（`KA_ACCOUNT_REDISPATCH_AFTER_SECONDS`），最多补投 3 次，仍未领取则 `failed`（`STUCK_REDISPATCH_EXHAUSTED`）并死信。**这一版仍有缺陷**：宽限期按时间猜，标题队列积压时会补出重复消息并误杀 24 个 Run（见 [r2](12-a3-r2-greeting-2000.md)）；已改为补投前先看队列在 Redis 里的深度，r3 复测通过（见 [r3](13-a3-r3-greeting-2000.md)） | **已修（r3 验证）** |
| 高 | 库里 71 个历史 queued Run，一旦补投能看到就会被重新执行、真的调模型 | 排队超过 1 小时（`KA_ACCOUNT_QUEUED_MAX_AGE_SECONDS`）的 Run 不补投，直接 `failed`（`STUCK_QUEUE_EXPIRED`）并死信，且先终止再补投。**这是自动的，不需要人工清库**；迁移后第一轮 sweeper 会把它们关掉 | **已修，r2 / r3 验证** |
| 高 | 瞬时 DB 连接失败导致 `execute_run` 抛错、消息被 ack | `execute_run` 遇 `OperationalError`（SQLAlchemy / psycopg）按 2–6 秒起、线性增长的 jitter 重试，最多 5 次，用完再抛，由 sweeper 兜底 | **已修** |
| 中 | 流式回复被缓冲（问题 3） | `_call_qwen` 在 async 循环里把 chunk 直接喂给 `_StreamAssembler`，`on_text` 边收边触发；测试用时间线断言 `on_text` 发生在流结束之前 | **已修** |
| 低 | `outbox_jobs` 的 `dispatched` 行不清理 | sweeper 每轮分批（默认 1000）删除超过 7 天、且对应 Run 已终态的行；仍 queued 的行保留。大表分区只作为后续建议，没有做 | **已修** |
| 可观测 | 看不到「堆积多久了」 | 新增 `agent_oldest_queued_age_seconds`（最老 queued Run 的等待秒数），与已有 `agent_stuck_runs`（>10 分钟的计数）互补 | **已加** |
| 中 | Qwen 单次调用无超时（问题 2） | 根因：`LLMSettings.timeout` 默认是 **7200 秒**，等于没有超时。现在每次 Qwen 调用总时长上限 30 秒（`KA_ACCOUNT_QWEN_TIMEOUT_SECONDS`），流开始后相邻 chunk 之间最长静默 10 秒（`KA_ACCOUNT_QWEN_CHUNK_IDLE_SECONDS`）；超时归类为可重试的 `PROVIDER_TIMEOUT`，路由会 fallback 到 DeepSeek。正常调用 2–6 秒，所以这两个值不影响正常请求。已知代价：一次超时前已经流出的半截文字会被 fallback 重新生成的回复替换 | **已修** |
| 高 | 连接预算 / 连接泄漏 | 核对后发现真正的问题是 **`execute_run` 每个任务都 `Database()`**：每次新建引擎（自己的连接池）并跑一遍 Alembic 检查，用完的池要等垃圾回收才释放。实测空闲的 worker 容器占着 **27 个连接，最老 2 小时 52 分**。改成每个 worker 子进程只建一次（带 pid 守卫）。同时按服务给连接池上限（worker 6+2、title-worker 3+1、sweeper 2+1，API 保持 10+20），见下方「连接预算」 | **已修，r2 验证：worker 空闲连接 27 → 5** |

实现上的几点取舍，写在这里避免之后误会：

- **宽限期不能取太短。** 一开始是 30 秒、最多 3 次，也就是排队超过约 2 分钟的 Run 会被判「补投耗尽」而失败。但排队深的时候 Run 只是在等，不是丢了。所以改成 120 秒，终止要等最后一次补投后再过一个宽限期，总共约 8 分钟。
- **状态机里 `queued` 没有到 `failed` 的边。** 终止补投耗尽的 Run 时经过一次 `running`（`attempts` 会加 1，事件原因是 `stuck_redispatch_exhausted` / `stuck_queue_expired`），没有为此改状态机。
- **`unclaimed_dispatches()` 保持只读**（测试驱动 `tests/test_execution_contract.py` 在用），生产只用会改状态的 `claim_unclaimed_dispatches()`。

回归测试（`tests/test_unclaimed_dispatch.py`、`tests/test_execute_run_db_retry.py`、`tests/test_llm.py`）：

- 105 条已完成的历史 dispatched outbox + 2 条真正卡住的 Run，断言 `limit=100` 仍能只返回那 2 条，并且 `redispatch_count` 只加 1；
- 补投 3 次后进入 `STUCK_REDISPATCH_EXHAUSTED`，有死信、outbox 记为 `failed`；
- 排队 5 小时的历史 Run 被关闭而不是被发布，排队 5 分钟的正常补投；排队 45 秒的不动；
- outbox 清理只删终态 Run 的行，保留仍 queued 的；
- 连接被拒时 `execute_run` 重试并带 jitter，次数用完后抛出；
- Qwen async 流的 `on_text` 在流结束之前就触发。

### 连接预算与连接泄漏（讲清楚这一块）

**先建立三个概念。**

1. SQLAlchemy 的 `Engine` 本身就是一个**连接池**。`create_engine()` 一次，就多了一个池；池里的连接用完是「还给池」，不是关闭，下次直接复用。池只有在引擎被 `dispose()` 或被垃圾回收时才会把连接真正关掉。
2. Postgres 的 `max_connections=100`，其中 3 个留给超级用户，应用能用 **97 个**。所有进程的池加起来不能超过它，否则就是 `FATAL: sorry, too many clients already`。
3. 每个池默认最多 `pool_size 10 + max_overflow 20 = 30` 个连接。「池的数量 × 30」才是理论上限。

**问题出在池的数量，不在池的大小。** 代码是这样的：

- `backend/db/database/__init__.py`：`Database.__init__` 里 `make_engine(...)` 新建引擎（新池），再 `run_migrations(...)` 做一次 Alembic 升级检查；
- `backend/celery/tasks.py`：`execute_run` 里原来是 `db = Database()`——**每执行一个任务就新建一个引擎**。

API 进程则不同：`deps.get_db` 把 `Database` 缓存在 `app.state` 上，整个进程只有一个池。所以只有 Celery worker 在泄漏。

**证据。** 压测结束后，对 Postgres 查 `pg_stat_activity` 按 `client_addr` 分组，worker 容器（跑了约 4 小时）占着 **27 个空闲连接，最老 2 小时 52 分**；title-worker 8 个；合计 36–47 个，而此时没有任何请求在跑。如果池只有一个，一个 5 子进程的 worker 不可能占到 27 个。需要如实说明的是：「用完的池要等垃圾回收」这个机制是根据代码和现象**推断**的，我们没有单独复现 GC 时序；确凿的是**修复前后的连接数对比**（下面）。

**这和那 1 秒的连接尖峰是什么关系。** 静止时就已占着 47 个，留给突发的余量只有大约 50 个。5 个 worker 子进程同时开新池和多条连接时，就可能触顶。这条因果我们**没有证明**：那一秒具体是谁占满的没有记录。能确认的是：修复前余量被无谓地吃掉了一半，修复后不再如此。

**怎么修的，两层。**

| 层 | 做法 | 作用 |
|---|---|---|
| 治本 | `tasks._database()`：每个 worker 子进程第一次用到时才建一个 `Database`，之后复用；记录 `os.getpid()`，进程号变了（fork 出的子进程）就重建 | 池从「每个任务一个」变成「每个子进程一个」，不再泄漏。pid 守卫是因为 Celery 的 prefork 会 fork 子进程，子进程不能继承父进程已经打开的连接 |
| 兜底 | `compose.yaml` 给各服务设 `KA_ACCOUNT_POOL_SIZE` / `KA_ACCOUNT_MAX_OVERFLOW` | 即使以后又有代码多建池，上限也是算过的，而不是 30 |

**预算怎么算的。**

| 服务 | 进程数 | 修复前每进程上限 | 修复前理论总和 | 修复后每进程上限 | 修复后总和 |
|---|---|---|---|---|---|
| worker（5 个子进程） | 5 | 30（且每个任务新建一个池） | 无上限（池随任务数增长） | 6+2 = 8 | 40 |
| title-worker（3 个子进程，r2 后由 2 调到 3） | 3 | 30 | 同上 | 3+1 = 4 | 12 |
| sweeper | 1 | 30 | 30 | 2+1 = 3 | 3 |
| api | 1 | 30 | 30 | 30（不变） | 30 |
| **合计** | | | | | **85 ≤ 97** |

worker 子进程一次只跑一个任务，但一个任务里有租约心跳线程和最多 5 个工具线程（`run_loop` 的 `ThreadPoolExecutor(max_workers=min(5, ...))`），所以给 6+2 而不是 1。85 离 97 只剩 12 个余量（给迁移、`psql`、测试进程用）。如果以后加 worker 子进程或 API 副本，要先重算这张表，或者调大 Postgres 的 `max_connections`。

**一个容易踩的坑**：Compose 的 YAML merge（`<<: *python`）是浅合并，服务里一旦写了自己的 `environment`，会把共享的那份**整个替换掉**，数据库连接串就丢了。所以改成 `environment: &python-env` 再在各服务里 `<<: *python-env` 重新合并，并用 `docker compose config` 确认三个服务既有池参数、也还有 `KA_ACCOUNT_DB_URL`。

**也量了一下「每个任务新建引擎」的耗时**：在容器里连续 8 次 `Database()`，首次 48ms，之后约 17ms；复用已有引擎取一个连接约 0.1ms。所以它每个任务多花约 **17ms**，**不是**我们在 S4 里看到的那 ≈2.5s 的来源（之前我猜过，现在排除）。这次修复的价值是**不泄漏连接**，不是提速。

## 简历建议

可以把这次事故写成一条完整的可靠性改进经历：

> 在 2000 条真实模型压测中定位到 2 个 Run 长期停在 `queued`：补投查询先对历史 `dispatched` outbox 做 `LIMIT 100`，再在 Python 过滤 Run 状态，导致真正卡住的 Run 排在第 3271 位后永远不可达。将 sweeper 改为以 `agent_runs` 为主表、数据库内先过滤再限量，加入 `queued_at` 部分索引、`FOR UPDATE SKIP LOCKED` 和幂等补投；补投带 120 秒宽限期、最多 3 次的上限，耗尽后失败并写入死信，排队超龄的历史 Run 直接关闭而不是重放；同时按保留期分批清理终态 Run 的 dispatched outbox，并用「105 条历史脏数据 + 2 条卡住 Run」的 Postgres 回归测试验证 `limit=100` 仍能命中。

面试时可以按这条线讲：

1. **发现**：2000 条压测里 2 条超时，查账本发现 outbox 是 `dispatched` 但 Run 是 `queued`、没有任何模型调用，不是性能问题。
2. **定位**：先想当然「sweeper 只标记不补投」，读代码后发现补投本来存在；用只读脚本验证 `limit=100` 返回空、`limit=100000` 返回 71 条，定位到「LIMIT 在过滤之前」。这类 bug 随表增长才暴露，单测行很少所以一直通过。
3. **修复**：不止改查询，还处理了并发（`SKIP LOCKED` + 同事务改状态）、重复投递（补投次数上限）、误杀（宽限期取长）、历史数据（超龄关闭，避免重放老 Run 调模型）、数据增长（outbox 保留期与批量清理，分区只作建议）。
4. **验证**：回归测试复现真实数据形态；测试里还抓到过两个自己的错误（截止点和时间戳相等导致匹配不到、测试里把首次投递也算进补投），说明断言是真的在查东西。
5. **补充**：同一次调查还带出另外两个问题并一起修了：DB 连接尖峰时任务不重试，以及我上一轮修 `Event loop is closed` 时把流式输出变成了缓冲。

## 下一步（已完成，见后续记录）

1. 重建并迁移到 `0026`：71 个历史 queued Run 走正常流程被关成 `STUCK_QUEUE_EXPIRED`，零模型调用（[r2](12-a3-r2-greeting-2000.md)）；
2. 同配置重跑 A3：r2 暴露补投逻辑把「在排队」当成「丢了」，修复后 r3 全部成功（[r3](13-a3-r3-greeting-2000.md)）；
3. 仍未被真实压测触发的：DB 连接被拒时的重试、Qwen 30s / 10s 超时，只有单测；
4. 实验 B（领域路径：Text2SQL、品牌检索、报表、采购）。Text2SQL 等会用到 DeepSeek 做 JSON，需要你确认是否使用真实测试集，我会提前说明。
