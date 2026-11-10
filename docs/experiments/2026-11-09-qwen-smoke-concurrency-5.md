# 实验：官方 Qwen 问候路径冒烟（并发 5）

- **第几次**：第 1 次（去掉 SJTU 之后的官方路由冒烟）
- **日期**：2026-11-09
- **代码**：工作树未提交；对照提交 `04e1800`（`main`），本轮含 SJTU 清理、worker 并发 8、`GATE_USER` 默认 5
- **原始文件**：[`exports/load-test-smoke-10.json`](exports/load-test-smoke-10.json)

## `GATE_USER` 是什么

`KA_ACCOUNT_GATE_USER` 是 Redis 上的**用户级 running 闸门**：同一 `(org_id, user_id)` 同时处于可占座状态的 run 数上限。

- 代码：`backend/services/entry_guard.py` 的 `acquire_run()` → 钥匙 `gate:running:user:{org}:{user}`
- 默认：现在是 **5**（本轮从 3 调到 5，与架构文档目标对齐）
- 领取时机：worker `work_once` 在 claim 之前先 `acquire_run`；拿不到座位就**不 claim**，直接返回
- 和什么不同：
  - `GATE_ORG` / `GATE_GLOBAL`：经销商 / 全局同时 running 上限
  - `GATE_MODEL`：每个模型 brand 同时调用上限
  - 会话锁（`ChatTurnBusy` / `SessionBusy`）：同一会话同时只跑一条 **chat**；这是 Postgres 行锁，不是 Redis 闸门

本轮冒烟 10 条请求共用同一个 `user-liancai-chen`。每条新会话还会额外建一条 `open`（标题）run，所以峰值大约是「HTTP 并发 × 2」条 run 去抢同一用户的 `GATE_USER` 座位。

## 配置

| 项 | 值 |
|---|---|
| 命令 | `./dev py scripts/load_test_chat.py --base-url http://api:8000 --partnership-id ps-liancai-01 --user-id user-liancai-chen --requests 10 --concurrency 5 --output docs/experiments/exports/load-test-smoke-10.json` |
| 消息模板 | `你好，这是第 {i} 条合成压测消息，请回复一句确认。` |
| PLAN / ANSWER | 官方 `qwen3.7-flash` |
| Worker `--concurrency` | 8 |
| `KA_ACCOUNT_GATE_USER` | 5（代码默认） |
| `KA_ACCOUNT_GATE_MODEL` | 32 |
| HTTP / completion timeout | 30s / 180s |

## 结果

| 指标 | 值 |
|---|---|
| 请求数 / 成功 / 超时 | 10 / 8 / 2 |
| 完成率 | 80% |
| 压测并发 | 5 |
| 成功 P50 | 9.31s |
| 成功 P95 | 10.91s |
| 成功 P99 | 未报（不足 300） |
| 观测 P95（含超时） | 180.32s（被 completion timeout 截断） |

对照：同模板上一轮（仍受校园路径影响、并发 1）是 7/10，成功尾部到约 178s。本轮成功样本已落在约 9～11 秒。

同窗口内库表汇总（`user-liancai-chen`，`23:39`）：

| run_type | succeeded | queued 未领 |
|---|---|---|
| chat | 8 | 2 |
| open | 4 | 6 |

冒烟只看会话里有没有 assistant 回写，所以 `open` 卡住不会单独记进压测失败；chat 卡住才会超时。

## 超时的两条

| # | session_id | chat run | open run |
|---|---|---|---|
| 5 | `a9afc902bc3d4c31a4e3f4be24418638` | `run_00d7f59ec78748c2ace069e522e3808e` | `run_fd71d991d81243e6a2c2e3b73da13fe7` |
| 9 | `3e7f5a96edaa4306bacfd6fdff13bace` | `run_59f2c30ce9d441c8aa42496f4b52103a` | `run_000e3333d8c94a06af43e371abbb3b8a` |

共同现象：

1. HTTP 入队 `202`，outbox 很快变成 `dispatched`
2. `agent_runs.status` 一直是 `queued`，`started_at` / `model_calls` 皆空
3. 约 10 分钟后出现内部事件 `run.queue_stale`，`reason=queue_unclaimed`
4. 没有 `ProviderError`、没有模型失败记录——根本没进到模型

## 根因（已核对代码 + 复现）

两段叠在一起：

### 1. 闸门打满时 Celery 直接 ACK，丢掉补跑机会

`work_once` 在 `acquire_run(...) is None` 时返回 `False`（不 raise）。  
`backend/celery/tasks.py` 的 `execute_run` 只对 `SessionBusy` 做 `self.retry`；其它 `False` 会 `return run_id`，Celery **ack** 掉消息。

冒烟并发 5、同一用户、每条消息再挂一条 `open`，瞬时可到约 10 个 acquire，超过 `GATE_USER=5`。打满时任务「成功结束」但 run 仍停在 `queued`，outbox 仍是 `dispatched`。

Worker 日志里对卡住的 chat run 有 `succeeded in 0.03s` 一类记录，符合「领不到座位立刻返回」。

### 2. sweeper 当时没在补投

设计上 `republish_unclaimed`（已 dispatched 且 run 仍 queued，超过约 30 秒）应再 `execute_run.delay`。  
调查时 `sweeper` 日志反复：

`failed to resolve host 'postgres': Name or service not known`

容器已 Up，但连不上 compose 网络里的 Postgres，补投链路断了。所以这些 run 一直停到冒烟 180s 超时，之后只留下 `queue_stale` 事件。

### 验证

闸门空闲后手动 `work_once`：

- 两条卡住的 chat、对应 open 都能在数秒内 `succeeded`
- 说明业务 payload / 模型路由本身没坏，是「领不到座位 + 没人再投递」

事后已 `docker compose up -d --force-recreate sweeper`，并手工跑通上述卡住的 run。

## 结论和下一步

1. **成功路径已经便宜且稳**：官方 Qwen 问候在并发 5 下，成功样本 P50/P95 约 9～11s。
2. **完成率缺口不是模型慢**，是入口闸门与 Celery ack 语义不匹配，再加上 sweeper 当时失联。
3. 建议修复（尚未做）：
   - `acquire_run` 失败时不要 ack：像 `SessionBusy` 一样短 countdown retry，或重新把 outbox 标回 `pending`
   - 冒烟/压测用多用户，或把 `open` 标题 run 排除出 `GATE_USER` / 会话高峰
   - compose 变更后确认 `sweeper` 与 `postgres` 在同一网络；监控 sweeper 异常
4. 下一轮：修完 ack 语义后再用相同模板、并发 5 做第 2 次，对比完成率。
