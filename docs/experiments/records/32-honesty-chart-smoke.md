# 记录 32：空月诚实出图冒烟

接记录 31。产品已含规划后代码打 `pie_category` / `trend` / `bar`、空结果跳过画图模型、画错维度丢掉、审阅「有画出的图才必须引用」。Worker 保持 `--concurrency=5`。不把这一刀算进 C 的 0–8 墙钟对比。

- **目的**：live 看 rpt-07 不再画出有值的品类饼图，聊天说 10 月缺数据；rpt-01 有数时仍出品类饼。
- **命令**：`./dev py docs/experiments/golden_set/run_golden.py --cases docs/experiments/golden_set/golden_v2.jsonl --phase pre --only rpt-01,rpt-07 --repeats 1 --concurrency 1 --timeout 300 --out docs/experiments/exports/honesty-smoke.jsonl --raw-dir docs/experiments/exports/honesty-smoke-raw`
- **原始文件**：[`honesty-smoke.jsonl`](../exports/honesty-smoke.jsonl)、[`honesty-smoke-raw/`](../exports/honesty-smoke-raw/)
- **当时 worker**：聊天队列 `--concurrency=5`。第一次尝试因容器 DNS/出网失败（`api.deepseek.com` 解析不到）两次 `client_timeout`；网络恢复后重跑。

## 结果

| 题 | 墙钟 | 费用 | 模型调用 / 失败 | 硬检查 |
|---|---:|---:|---|---|
| rpt-01 | 127.1 s | ¥0.198 | 43 / 3 | pass（品类饼 Chocolate / Biscuits & Cakes / Grocery） |
| rpt-07 | 117.9 s | ¥0.196 | 48 / 2 | pass（`no_data_stated` + `no_pie_with_values`） |

2/2 有 assistant 回复，0 客户端超时。评分器 `FAILED: none`。

rpt-07 聊天写了品类饼图未生成，并保留 5–8 月趋势和未完成采购单图；没有再出现 Cerise 那种产品饼。第一句仍是「月报已生成」，随后才说 10 月缺数据，没有整份报告短路。

## 下一步

Qwen 审阅仍是单独一刀，本记录之后再换。
