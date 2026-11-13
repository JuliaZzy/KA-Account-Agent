# 记录 29：C 之后按记录 26 混跑，看有没有撞 `GATE_ORG`

接记录 26、28。产品已含 C 的刀 0–4（报表预算、审阅 2 轮批量改章、确定性 risk_flags、四节洞察一次生成、检索进第一波）。Worker 保持 `--concurrency=5`，题目和提供负载与实验 D 的 Worker=5 组相同。

- **目的**：在混跑里看组织运行闸门有没有被第一波（8 data + 2–3 search）顶满。不是再比 Worker=3/10。
- **命令**：`docs/experiments/golden_set/run_c_gate.sh`（口径同 `run_d.sh` 的 Worker=5：客户端并发 10、超时 300 s；输出前缀 `c-gate-w5`，不覆盖 `d-w5-*`）。跑的同时每 2 s 采一次 Redis `gate:*` 的 ZCARD。
- **原始文件**：[`c-gate-w5-pre.jsonl`](../exports/c-gate-w5-pre.jsonl)、[`c-gate-w5-post.jsonl`](../exports/c-gate-w5-post.jsonl)、[`c-gate-w5-gates.tsv`](../exports/c-gate-w5-gates.tsv)、[`c-gate-w5-summary.json`](../exports/c-gate-w5-summary.json)、[`c-gate-w5-raw/`](../exports/c-gate-w5-pre-raw/)（pre/post 各一份 raw）。
- **当时 worker**：聊天队列 `--concurrency=5`；title-worker 仍是 3。`GATE_ORG=10`，`GATE_USER=5`，`GATE_MODEL=32`。样本 50 次里绝大多数是 `user-liancai-chen`，只有 `t2s-10` 走海纳。

## 结果

| 组 | 吞吐（次/分） | 墙钟 | 出错 / 超时 | 轻任务 P50（含排队） | 轻任务排队 P50 / P95 | 完整月报 P50 | 成本 |
|---|---:|---:|---:|---:|---|---:|---:|
| D Worker=5（记录 26） | 8.06 | 372 s | 0 / 0 | 22.08 s | 7.62 / 44.95 s | 225 s | ¥1.668 |
| 本轮 C 后 Worker=5 | 8.57 | 350 s | 0 / 0 | 18.55 s | 11.27 / 30.17 s | 174 s | ¥1.330 |

50/50 有回复，0 客户端超时。完整月报 6 次墙钟：143、146、174、184、194、271 s（`plan-02` 第 1 次是尾巴）。轻任务最长是 `po-01`（127 / 135 s，排队 27 / 36 s），还是月报占住 5 个座位时短单排在后面，和记录 26 一样。

对照 D 的 Worker=5：吞吐和轻任务 P50 差不多；月报中位从 225 s 降到 174 s，整组成本 ¥1.67 → ¥1.33，主要来自已经落地的 C（洞察合成、检索入波、审阅收口），不是这次闸门采样。不要把空月诚实闸算进这笔差。

## 闸门占用（Redis 787 个样本）

| 键 | 上限 | 峰值 | 打在峰值上的样本 |
|---|---:|---:|---:|
| `gate:running:org:dist-liancai` | 10 | **5** | 61% |
| `gate:running:user:dist-liancai:user-liancai-chen` | 5 | **5** | 61% |
| `gate:running:global` | 100 | 5 | 65% |
| `gate:running:org:dist-haina` | 10 | 2 | （`t2s-10` 两趟） |
| `gate:model:qwen` | 32 | 8 | 7% |
| `gate:model:deepseek` | 32 | 5 | 15% |

**没有撞 `GATE_ORG=10`。** 联采组织峰值一直是 5，等于 Worker 座位，也等于该用户的 `GATE_USER`。模型闸门峰值 8，离 32 还远。worker 日志里没有 `Retry in … gate_full`：5 个进程领到任务时座位还空着，第 6 个任务等在 Celery 队列里，轮不到去抢 Redis 闸门。

记录 28 里「一波 8 data + 2–3 search 会顶满 org 座位」在 Worker=5 下不成立。父 Run `waiting_for_tasks` 时已经放掉座位；同一时刻最多 5 个子 Run 在跑，组织计数到不了 10。

父 Run 的 `run.enqueued` 多于 1 次是派波之后被叫醒，不是闸门拒绝。6 份月报都是 `run.started` × 2，0 次 `run.requeued_by_sweeper`。

## 检索入波（混跑）

六份月报第一波都是 8 data + 3 search。`s*` 仍排在 `q*` 后面，五个座位先被 data 占满：

| 题 | 检索相对 q1 开工 | 与仍在跑的 data 重叠 |
|---|---:|---:|
| rpt-01 ×2 | 4.4 / 6.3 s | 0.9 / 0.9 s |
| rpt-07 ×2 | 7.1 / 5.5 s | 0.9 / 0.6 s |
| plan-02 ×2 | 5.0 / 5.8 s | 0.8 / 0.8 s |

和记录 28 并发 1 冒烟同一量级（晚 5–7 s，重叠不到 1 s）。混跑没有让检索更早开工，也没有让组织闸门顶满。

## 结论

1. **这套提供负载下，卡住的是 Worker=5 / `GATE_USER=5`，不是 `GATE_ORG=10`。** 题目几乎都是同一个联采用户；即使用户闸门放宽，Worker=5 也只能占 5 个组织座位。
2. **检索进第一波不会在 Worker=5 上撞组织闸门**，它只是和后半段 data 抢同一批座位。
3. 相对记录 26 的 Worker=5，混跑吞吐几乎没变，完整月报中位短了约 50 s。C 目前改的是月报内部，不是队列深度。

## 遗留

1. 真要看到 `GATE_ORG` 打满，需要 Worker ≥ 10，并且同一 org 里同时有足够多的用户越过 `GATE_USER=5`。记录 26 的 Worker=10 组没有采 Redis，当时「5→10 没收益」更可能先撞的是用户闸门，而不是组织闸门。
2. 若要检索和第一批 data 同时开工，仍然是加 worker 座位，或派发时不要把 `s*` 全排在 `q*` 后面（记录 28）。
3. 刀 5–8（章节并行写、图表与洞察重叠、模板 SQL、purpose 分级）见记录 30。
