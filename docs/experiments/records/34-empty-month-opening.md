# 记录 34：空月聊天第一句钉死

接记录 33。Qwen 审阅保留。Worker 保持 `--concurrency=5`。不把这一刀算进 C 的 0–8 墙钟对比。

- **目的**：用户点名的月份没有采购/动销时，聊天第一句必须是「{期间}没有数据」；右侧报告仍可写近半年。
- **做法**：指标合计里没有该 YYYY-MM 就打 `named_period_empty`；ANSWER 若没按这句话开口，代码补上。顺手把 `ROUND(SUM(...), 2)` 改成 `CAST(... AS numeric)`，否则 Postgres 对 `double precision` 直接报错，标记打不上。
- **命令**：`./dev py docs/experiments/golden_set/run_golden.py --cases docs/experiments/golden_set/golden_v2.jsonl --phase pre --only rpt-01,rpt-07 --repeats 1 --concurrency 1 --timeout 300 --out docs/experiments/exports/empty-opening-smoke.jsonl --raw-dir docs/experiments/exports/empty-opening-smoke-raw`
- **原始文件**：[`empty-opening-smoke.jsonl`](../exports/empty-opening-smoke.jsonl)、[`empty-opening-smoke-raw/`](../exports/empty-opening-smoke-raw/)
- **当时 worker**：聊天队列 `--concurrency=5`（换代码后重建）。

## 结果

| 题 | 墙钟 | 费用 | 模型调用 / 失败 | 审阅 | 硬检查 | 开口 |
|---|---:|---:|---|---|---|---|
| rpt-01 | 230 s | ¥0.110 | 51 / 6 | Qwen 6 次成功，88 s | pass | 「2026年9月采购月报已生成」（有数，不改） |
| rpt-07 | 115 s | ¥0.078 | 47 / 1 | Qwen 6 次成功，60 s | pass（`no_data_stated` + `no_pie_with_values`） | **「2026年10月没有数据」** |

2/2 有 assistant 回复，0 客户端超时。审阅没有回退 DeepSeek。rpt-07 仍跳过品类饼，趋势图还在。
