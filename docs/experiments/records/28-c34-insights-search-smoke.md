# 记录 28：C 洞察合成 + 检索入波 冒烟

接记录 25–27。产品已改：四节洞察一次生成、失败隔离；规划里的 `web_search` 和 data 子 Run 同一波派发。审阅仍是 2 轮、有问题的章节一次改完；风险 flags 仍是确定性 Python。

- **目的**：链通，并看并发 1 时检索和取数是否重叠。不测 Worker=5 混跑撞 `GATE_ORG`。
- **命令**：`./dev py docs/experiments/golden_set/run_golden.py --cases docs/experiments/golden_set/golden_v2.jsonl --phase pre --only rpt-01,rpt-07,plan-02 --repeats 1 --concurrency 1 --timeout 300 --out docs/experiments/exports/c34-smoke.jsonl --raw-dir docs/experiments/exports/c34-smoke-raw`
- **原始文件**：[`c34-smoke.jsonl`](../exports/c34-smoke.jsonl)、[`c34-smoke.log`](../exports/c34-smoke.log)、[`c34-smoke-raw/`](../exports/c34-smoke-raw/)
- **当时 worker**：聊天队列 `--concurrency=5`（compose 默认）。`celery inspect` 里先出现的 `max-concurrency=3` 是 **title-worker**（只跑 `ka_account.title`），不是这份冒烟用的 worker。`GATE_ORG=10`。

## 结果

| 题 | 墙钟 | 账本 | 费用 | 模型调用 / 失败 | 硬检查 | 第一波 |
|---|---:|---:|---:|---|---|---|
| rpt-01 | 128.7 s | 127.8 s | ¥0.183 | 50 / 2 | pass | q1–q8 + s1–s3 |
| rpt-07 | 161.4 s | 161.0 s | ¥0.237 | 61 / 6 | fail（`no_data_stated`） | q1–q9 + s1–s3 |
| plan-02 | 149.3 s | 148.9 s | ¥0.229 | 51 / 2 | pass | q1–q8 + s1–s2 |

3/3 有 assistant 回复，0 客户端超时。`rpt-07` 的 `no_pie_with_values` 过了（没画出有值的品类饼图），聊天没说「10 月没有数据」，只报了洞察 `NameError`。这是空月诚实闸，不进 C 归因。

对照 rptfix（记录 27，漏记补上后）：墙钟仍在 129–161 s，费用 ¥0.18–0.24（当时约 ¥0.22–0.28）。合并洞察没有把完整月报砍到分钟级，审阅仍在。

## 洞察合成

| 题 | `code_wizards` | `code_wizard`（单章） | 备注 |
|---|---|---|---|
| rpt-01 | 1 次成功（9.1 s） | 0 | 另有 `code.fix` |
| rpt-07 | 1 次 JSON 解析失败 + 1 次成功（18.6 s） | 0 | 失败隔离：整批重试，不是四章各写一次 |
| plan-02 | 1 次成功（9.3 s） | 0 | |

前四节正文都进了账本（`report_section` 6–8 次，含摘要/风险）。

## 检索入波（并发 1）

三份计划都把 `s*` 和 `q*` 写进同一波，`depends_on=[]`，子 Run 全部 succeeded。检索本身 0.3–0.4 s。

Worker=5 时第一波仍是 10–12 个孩子（8–9 条 data + 2–3 条 search）。计划里 `q*` 写在 `s*` 前面，五个座位先被 data 占满：`rpt-01` 在 1 s 内同时跑 q1–q5，`s1` 要等 q4 结束才开工（约晚 7 s）。检索本身 0.3–0.4 s，和还在跑的 data 重叠 0.4–0.9 s。检索**在同一波里**，但开工被 data 挤到后面，不是串在 SQL 全部结束之后。

并发 1 占不满 `GATE_ORG=10`（父 Run parked + 同时最多 5 个子 Run）。

## 下一步

- 不必为 Worker=5 重跑这份冒烟。
- `GATE_ORG` 混跑见记录 29：Worker=5 时组织峰值是 5，没撞 10；卡住的是 Worker / `GATE_USER`。
- 若要检索和第一批 data **同时开工**，需要更多 worker 座位，或派发时不要把 `s*` 全部排在 `q*` 后面。
