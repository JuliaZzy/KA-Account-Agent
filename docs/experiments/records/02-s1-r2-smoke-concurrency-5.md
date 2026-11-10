# 实验：官方 Qwen 问候路径冒烟（并发 5，第 2 次）

- **第几次**：第 2 次（相对 [`01-s1-smoke-concurrency-5.md`](01-s1-smoke-concurrency-5.md)）
- **日期**：2026-11-09（UTC 2026-11-10T01:06Z 附近跑完）
- **对照**：第 1 次 10/8/2（完成率 80%）；本轮在标题隔离 + soft-busy retry + sweeper DB 探活之后
- **原始文件**：[`exports/load-test-smoke-10-r2.json`](../exports/load-test-smoke-10-r2.json)

## 相对第 1 次多了什么

见第 1 次记录的「已修复」：

- 入队临时标题；chat 终态后再投 OPEN 到 `ka_account.title`（不占 chat `GATE_USER`）
- `acquire_run` / 队列关闭 → `SoftBusy`，Celery `retry`，不再 ACK 丢掉补跑
- sweeper healthcheck 探测 Postgres

## 配置

| 项 | 值 |
|---|---|
| 命令 | `./dev py scripts/load_test_chat.py --base-url http://api:8000 --partnership-id ps-liancai-01 --user-id user-liancai-chen --requests 10 --concurrency 5 --output docs/experiments/exports/load-test-smoke-10-r2.json` |
| 消息模板 | `你好，这是第 {i} 条合成压测消息，请回复一句确认。`（与第 1 次相同） |
| PLAN / ANSWER | 官方 `qwen3.7-flash` |
| Worker `--concurrency` | 5（`ka_account` 队列） |
| title-worker | concurrency 2，队列 `ka_account.title` |
| `KA_ACCOUNT_GATE_USER` | 5 |
| HTTP / completion timeout | 默认（与脚本一致） |

## 结果

| 指标 | 第 1 次 | 第 2 次 |
|---|---|---|
| 请求数 / 成功 / 超时 | 10 / 8 / 2 | 10 / 10 / 0 |
| 完成率 | 80% | **100%** |
| 成功 P50 | 9.31s | 4.07s |
| 成功 P95 | 10.91s | 12.45s |
| 观测 max | 180.32s（超时截断） | 12.45s |

同窗口库表（`user-liancai-chen`，近 15 分钟）：

| run_type | succeeded | queued 未领 |
|---|---|---|
| chat | 10 | 0 |
| open | 10 | 0 |

sweeper 健康状态：`healthy`（Postgres `select 1` 探活通过）。

### 本轮 10 条会话的 Token 与花费（按 2026-11-09 官方价格，北京时间 09:05 高峰时段）

| Run 类型 | 模型 | 成功调用 | Prompt Tokens | Cache Hit（命中率） | Cache Miss | Completion Tokens | 实际花费 | 无缓存原价 |
|---|---|---|---|---|---|---|---|---|
| `chat` | `deepseek-flash`（高峰） | 15 | 13,985 | 7,424（53.1%） | 6,561 | 155 | ¥0.014659 | ¥0.029210 |
| `chat` | `qwen3.7-flash` | 5 | 12,280 | 2,176（17.7%） | 10,104 | 25 | ¥0.002128 | ¥0.002476 |
| `open` | `deepseek-flash`（高峰） | 10 | 3,790 | 1,024（27.0%） | 2,766 | 140 | ¥0.006693 | ¥0.008700 |
| **合计** | — | **30** | **30,055** | **10,624（35.3%）** | **19,431** | **320** | **¥0.023480（2.35 分）** | **¥0.040386（省 41.9%）** |

- **单请求总成本**：**¥0.002348 / 条**（`chat` ¥0.001679 + `open` ¥0.000669）。

## 结论

1. 相同模板、相同并发下，完成率从 80% → 100%；没有再出现「outbox dispatched + run 一直 queued」的卡死。
2. 成功尾部 P95 略高于第 1 次成功样本（约 12.5s vs 10.9s），但仍远好于第 1 次被 180s 超时拖坏的观测尾部；P50 更低（约 4s），符合闸门打满时任务会短暂 retry 再跑通、而不是 ACK 丢弃。
3. open 也全部 succeeded，说明标题队列与 chat 闸门隔离后，标题 refine 不再和问候 chat 抢同一用户座位。
