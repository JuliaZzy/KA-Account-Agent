# 记录 30：C 刀 5–8 之后的并发 1 冒烟 + Worker=5 混跑

接记录 28、29。产品已含刀 5–8：进程内图表∥洞察、四节正文并行写、模板化 metrics SQL、purpose 分级（`report.write` / `report.review` / `report.optional`）。审阅仍是 2 轮；风险 flags 仍是确定性 Python。跑之前重启了聊天 worker（否则还是 7 小时前的代码）。

- **目的**：两张表都看。并发 1 看墙钟、第一波 `q*` 是否少了 metrics、图∥洞察和并行写是否真的叠上。混跑看 `GATE_ORG` 和 `GATE_MODEL`（进程内两路 LLM 抢的是模型闸门，不是组织闸门）。
- **当时 worker**：聊天队列 `--concurrency=5`；title-worker 仍是 3。`GATE_ORG=10`，`GATE_USER=5`，`GATE_MODEL=32`。
- **不覆盖**记录 28/29 的 `c34-smoke-*`、`c-gate-w5-*`，也不覆盖 `d-w5-*`。

## 1. 并发 1 冒烟

- **命令**：`./dev py docs/experiments/golden_set/run_golden.py --cases docs/experiments/golden_set/golden_v2.jsonl --phase pre --only rpt-01,rpt-07,plan-02 --repeats 1 --concurrency 1 --timeout 300 --out docs/experiments/exports/c58-smoke.jsonl --raw-dir docs/experiments/exports/c58-smoke-raw`
- **原始文件**：[`c58-smoke.jsonl`](../exports/c58-smoke.jsonl)、[`c58-smoke.log`](../exports/c58-smoke.log)、[`c58-smoke-raw/`](../exports/c58-smoke-raw/)

| 题 | 墙钟（记录 28 → 本轮） | 账本 | 费用 | 模型调用 / 失败 | 硬检查 | 第一波 |
|---|---:|---:|---:|---|---|---|
| rpt-01 | 128.7 → **88.5 s** | 87.5 s | ¥0.177 | 41 / 0 | pass | q1–q6 + s1–s2 |
| rpt-07 | 161.4 → **126.6 s** | 126.2 s | ¥0.185 | 42 / 3 | fail（`no_pie_with_values`） | q1–q6 + s1–s3 |
| plan-02 | 149.3 → **152.3 s** | 151.8 s | ¥0.196 | 39 / 5 | pass | q1–q5 + s1–s3 |

3/3 有 assistant 回复，0 客户端超时。`rpt-07` 的 `no_data_stated` 过了（聊天写了 10 月没有匹配的采购单），但画了一张带值的品类/产品饼图（Cerise 等，不是空月）。这是空月诚实闸，不进 C 归因。

`plan-02` 墙钟没短：`code_wizards` 一次失败后走了 Qwen 重试（18.4 s），把并行省下的时间吃回去了。不要拿这一份当刀 5–8 无效。

### 模板化 metrics

记录 28 第一波是 8–9 条 data。本轮规划里 **没有 metrics 名义的 `q*`**（`metrics_named_q=0`），data 子 Run 掉到 5–6 条。关键指标走 `run_statement` 模板 SQL，不再占第一波座位。

### 图∥洞察、并行写（账本 `created_at`）

`model_calls` 没有 `started_at`，用插入时间倒推。`rpt-01`：

- `report_charts`（3.8 s + 2.3 s）和 `code_wizards`（7.3 s）同一时刻开工，图表先结束，洞察大约重叠 **6 s**。
- 前四节 `report_section` 的 `created_at` 落在 2.1 s 窗口里（02.77 / 03.48 / 03.84 / 04.84），各自耗时 2.5–4.5 s，不是串行。摘要和风险仍串在后面（间隔 ≈ 各自 latency）。

账本 purpose 已拆开：`report.write` 6、`report.review` 6、`report.optional` 4（计划/图/洞察）。purpose 分级本身不缩短墙钟，短的是并行和少掉的 metrics `q*`。

## 2. Worker=5、客户端并发 10 混跑

- **命令**：`docs/experiments/golden_set/run_c58_gate.sh`（口径同记录 26 / 29：客户端并发 10、超时 300 s；32 pre + 18 post）。跑的同时每 2 s 采一次 Redis `gate:*` 的 ZCARD。
- **原始文件**：[`c58-gate-w5-pre.jsonl`](../exports/c58-gate-w5-pre.jsonl)、[`c58-gate-w5-post.jsonl`](../exports/c58-gate-w5-post.jsonl)、[`c58-gate-w5-gates.tsv`](../exports/c58-gate-w5-gates.tsv)、[`c58-gate-w5-summary.json`](../exports/c58-gate-w5-summary.json)、pre/post 各一份 raw。

| 组 | 吞吐（次/分） | 墙钟 | 出错 / 超时 | 轻任务 P50（含排队） | 轻任务排队 P50 / P95 | 完整月报 P50 | 成本 |
|---|---:|---:|---:|---:|---|---:|---:|
| D Worker=5（记录 26） | 8.06 | 372 s | 0 / 0 | 22.08 s | 7.62 / 44.95 s | 225 s | ¥1.668 |
| C 刀 0–4（记录 29） | 8.57 | 350 s | 0 / 0 | 18.55 s | 11.27 / 30.17 s | 174 s | ¥1.330 |
| 本轮刀 5–8 | **10.42** | **288 s** | 0 / 0 | **15.98 s** | **6.46 / 21.18 s** | **152 s** | **¥1.238** |

50/50 有回复，0 客户端超时。完整月报 6 次墙钟：140、147、152、158、190、219 s（`rpt-07` 第 2 次是尾巴）。worker 日志里没有 `Retry in … gate_full`。

相对记录 29：整组墙钟 350 → 288 s，月报中位 174 → 152 s，成本 ¥1.33 → ¥1.24。轻任务 P50 也短了一截，主要是月报少占座位、短单少排。不要把 `rpt-07` 的饼图诚实闸算进这笔差。

### 闸门占用（Redis 647 行 / 143 个时间戳）

| 键 | 上限 | 峰值 | 打在峰值上的时间戳 |
|---|---:|---:|---:|
| `gate:running:org:dist-liancai` | 10 | **5** | 68% |
| `gate:running:user:dist-liancai:user-liancai-chen` | 5 | **5** | 66% |
| `gate:running:global` | 100 | 5 | 71% |
| `gate:running:org:dist-haina` | 10 | 2 | （`t2s-10`） |
| `gate:model:qwen` | 32 | 8 | 1%（≥6 的占 8%） |
| `gate:model:deepseek` | 32 | **10** | 1 个时间戳；≥6 的 6 个 |

**仍然没有撞 `GATE_ORG=10`。** 组织和用户峰值还是 5，等于 Worker 座位。这和记录 29 同一结论：卡住的是 Worker=5 / `GATE_USER=5`。

**`GATE_MODEL` 动了。** 记录 29 里 DeepSeek 峰值是 5（一个 worker 同时只打一发）。本轮峰值 10：进程内图∥洞察和四节并行写会在同一个 worker 座位上再占模型闸门。离 32 还远，没有 `gate_full`。Qwen 峰值仍是 8（降级/修复路径），不是这四刀的主信号。

### 检索入波（混跑）

第一波 data 从记录 29 的一律 8 条降到 5–7 条（模板化 metrics）。`s*` 仍排在 `q*` 后面：

| 题 | 检索相对 q1 开工 | 与仍在跑的 data 重叠 |
|---|---:|---:|
| rpt-01 ×2 | 2.3 / 2.4 s | 0.53 / 0.55 s |
| rpt-07 ×2 | 3.1 / **19.8 s** | 0.59 / 0.56 s |
| plan-02 ×2 | 3.3 / 2.3 s | 0.58 / 0.76 s |

中位滞后 5.5 → 2.4 s：少了两条 metrics `q*` 之后，五个座位里的 data 更快结束，检索更早轮到。`rpt-07` 第 2 次 19.8 s 是混跑抢位的尾巴，不是检索又被串到 SQL 全部结束之后。重叠仍不到 1 s。

## 结论

1. **刀 5–6 在并发 1 和混跑里都叠上了**：四节正文并行写、图表与洞察同时开工；混跑里 DeepSeek `GATE_MODEL` 从 5 抬到 10，组织闸门没动。
2. **刀 7 减掉了第一波 metrics `q*`**（8–9 → 5–6）。检索中位更早开工，月报中位再短约 20 s。
3. **刀 8 只改记账/路由标签**，墙钟不靠它。账本里已经能按 `report.write` / `review` / `optional` 摊开。
4. **`GATE_ORG=10` 在 Worker=5 单用户下仍然打不满。** Worker=12 + 联采三用户见记录 31。

## 遗留

1. Worker≥10 / 多用户打满 `GATE_ORG` 见记录 31。检索不要全部排在 `q*` 后面，还在。
2. Qwen 审阅仍是单独一刀，本轮没动。
3. `rpt-07` 空月仍会画出带值的饼图；诚实闸不并进 C。
