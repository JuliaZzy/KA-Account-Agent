# 实验：冒烟 S2 — 单 org 多用户（问候路径，20 条）

- **第几次**：冒烟 S2（相对 S1 单用户 10）
- **日期**：2026-11-09（UTC 2026-11-10T01:52Z 附近跑完）
- **代码**：工作树含 soft-busy / 标题隔离；对照 `983f7ed`
- **原始文件**：[`exports/load-test-smoke-s2-20.json`](../exports/load-test-smoke-s2-20.json)、[`exports/load-test-smoke-s2-20.log`](../exports/load-test-smoke-s2-20.log)

## 目的

同一 `partnership`（`ps-liancai-01`）下轮转 3 个用户，验证 `GATE_USER` 不再被单用户打满拖垮，完成率与延迟仍健康。

## 配置

| 项 | 值 |
|---|---|
| 命令 | `./dev py scripts/load_test_chat.py --base-url http://api:8000 --partnership-id ps-liancai-01 --user-id user-liancai-chen --user-ids user-liancai-chen,user-liancai-lin,user-liancai-zhou --requests 20 --concurrency 5 --output docs/experiments/exports/load-test-smoke-s2-20.json` |
| 消息模板 | `你好，这是第 {i} 条合成压测消息，请回复一句确认。` |
| 用户分布 | chen 7 / lin 7 / zhou 6 |
| Worker `--concurrency` | 5 |
| title-worker | 2 |
| `KA_ACCOUNT_GATE_USER` | 5 |
| HTTP / completion timeout | 30s / 180s |

## 结果

| 指标 | S1 r2（单用户 10） | S2（多用户 20） |
|---|---|---|
| 请求 / 成功 / 超时 | 10 / 10 / 0 | **20 / 20 / 0** |
| 完成率 | 100% | **100%** |
| 成功 P50 | 4.07s | **3.72s** |
| 成功 P95 | 12.45s | **6.77s** |
| 成功 P99 | 未报 | 未报 |

库表（近 20 分钟，三用户合计）：

| run_type | succeeded | queued |
|---|---|---|
| chat | 20 | 0 |
| open | 20 | 0 |

sweeper：`healthy`。

### 本轮 20 条会话的 Token 与花费（按 2026-11-09 官方价格，北京时间 09:52 高峰时段）

| Run 类型 | 模型 | 成功调用 | Prompt Tokens | Cache Hit（命中率） | Cache Miss | Completion Tokens | 实际花费 | 无缓存原价 |
|---|---|---|---|---|---|---|---|---|
| `chat` | `deepseek-flash`（高峰） | 35 | 39,315 | 32,000（81.4%） | 7,315 | 331 | ¥0.018558 | ¥0.081278 |
| `chat` | `qwen3.7-flash` | 5 | 12,280 | 6,528（53.2%） | 5,752 | 33 | ¥0.001438 | ¥0.002482 |
| `open` | `deepseek-flash`（高峰） | 20 | 7,580 | 2,560（33.8%） | 5,020 | 280 | ¥0.012382 | ¥0.017400 |
| **合计** | — | **60** | **59,175** | **41,088（69.4%）** | **18,087** | **644** | **¥0.032378（3.24 分）** | **¥0.101160（省 68.0%）** |

- **单请求总成本**：**¥0.001619 / 条**（`chat` ¥0.001000 + `open` ¥0.000619；当时 Qwen async stream 尚未修复，多数 ANSWER / 标题 fallback 到高峰期 `deepseek-flash`）。

## 结论

1. 多用户后成功 P95 从单用户 r2 的 ~12.5s 降到 ~6.8s，尾部不再明显出现「第二波等 soft-busy」那种 11～12s 簇。
2. 完成率仍 100%，chat/open 无残留 queued，说明标题隔离 + soft-busy 在多用户下也站得住。
3. 下一步：冒烟 **S3 多 org**；通过后进入实验 A（50 → 100 → 500，默认固定本轮这种单 org 多用户分布）。
