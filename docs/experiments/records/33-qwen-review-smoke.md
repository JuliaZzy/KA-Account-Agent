# 记录 33：审阅改走 Qwen 冒烟

接记录 32。`report.review` / `report_revise` 首选 `qwen3.7-flash`，失败再回 DeepSeek。正文、出图、规划仍走 DeepSeek。Worker 保持 `--concurrency=5`。不把这一刀算进 C 的 0–8 墙钟对比。

- **目的**：确认审阅真正打到 Qwen，硬检查不比诚实闸那一轮差。
- **命令**：`./dev py docs/experiments/golden_set/run_golden.py --cases docs/experiments/golden_set/golden_v2.jsonl --phase pre --only rpt-01,rpt-07 --repeats 1 --concurrency 1 --timeout 300 --out docs/experiments/exports/qwen-review-smoke.jsonl --raw-dir docs/experiments/exports/qwen-review-smoke-raw`
- **原始文件**：[`qwen-review-smoke.jsonl`](../exports/qwen-review-smoke.jsonl)、[`qwen-review-smoke-raw/`](../exports/qwen-review-smoke-raw/)
- **当时 worker**：聊天队列 `--concurrency=5`（换代码后重建）。

## 结果

| 题 | 墙钟（记录 32 → 本轮） | 费用 | 模型调用 / 失败 | 审阅 | 硬检查 |
|---|---:|---:|---|---|---|
| rpt-01 | 127 → **205 s** | ¥0.198 → **¥0.123** | 49 / 4 | Qwen 6 次成功，108 s | pass |
| rpt-07 | 118 → **142 s** | ¥0.196 → **¥0.110** | 48 / 0 | Qwen 6 次成功，99 s | fail（`no_data_stated`） |

2/2 有 assistant 回复，0 客户端超时。审阅没有回退 DeepSeek。

rpt-01 仍画出 Chocolate / Biscuits & Cakes / Grocery 品类饼。rpt-07 没有品类饼（`pie_category` 因查询维度对不上被跳过），趋势图还在。聊天写了「品类饼图未能生成」「本月数据不完整」，但没用评分器认的「没有数据」，所以 `no_data_stated` 失败。这是 ANSWER 用词，不是又画出假饼。

墙钟变长、费用变低：审阅六次全在 Qwen，单次比 DeepSeek 慢，单价更低。
