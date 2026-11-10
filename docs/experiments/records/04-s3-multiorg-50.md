# 实验：冒烟 S3 — 多 org 多用户（问候路径，50 条）

- **第几次**：冒烟 S3（相对 S2 单 org 多用户 20）
- **日期**：2026-11-09（UTC 2026-11-10T01:57Z 附近跑完）
- **代码**：工作树含 soft-busy / 标题隔离 / `--user-ids`+`--partnership-ids`；对照 `983f7ed`
- **原始文件**：[`exports/load-test-smoke-s3-50.json`](../exports/load-test-smoke-s3-50.json)、[`exports/load-test-smoke-s3-50.log`](../exports/load-test-smoke-s3-50.log)

## 目的

跨两个 distributor（`dist-liancai` + `dist-haina`）轮转用户与 partnership，验证 `GATE_ORG` / 全局闸门下链仍通，完成率不掉。

## 配置

| 项 | 值 |
|---|---|
| 命令 | 见下 |
| 消息模板 | `你好，这是第 {i} 条合成压测消息，请回复一句确认。` |
| 租户 | 6 对 `(partnership, user)` 轮转：联采 ×3 + 海纳 ×3 |
| 请求 / 并发 | 50 / 5 |
| Worker / title-worker | 5 / 2 |
| `GATE_USER` | 5 |
| HTTP / completion timeout | 30s / 180s |

```bash
./dev py scripts/load_test_chat.py \
  --base-url http://api:8000 \
  --partnership-id ps-liancai-01 \
  --user-id user-liancai-chen \
  --partnership-ids ps-liancai-01,ps-liancai-01,ps-liancai-01,ps-haina-activia,ps-haina-lavazza,ps-haina-lipton \
  --user-ids user-liancai-chen,user-liancai-lin,user-liancai-zhou,user-haina-zhao,user-haina-wu,user-haina-zheng \
  --requests 50 --concurrency 5 \
  --output docs/experiments/exports/load-test-smoke-s3-50.json
```

用户条数：chen/lin/zhou = 9/9/8；zhao/wu/zheng = 8/8/8。  
partnership：`ps-liancai-01` 26 条；三个海纳各 8 条。

## 结果

| 指标 | S2（多用户 20） | S3（多 org 50） |
|---|---|---|
| 请求 / 成功 / 超时 | 20 / 20 / 0 | **50 / 50 / 0** |
| 完成率 | 100% | **100%** |
| 成功 P50 | 3.72s | **6.16s** |
| 成功 P95 | 6.77s | **35.22s** |
| 成功 max | 6.82s | **43.46s** |
| 成功 P99 | 未报 | 未报 |

本轮数据库记录（`agent_runs`，两 org 用户合计，按压测时间窗筛）：chat/open 均为 succeeded，无 queued 残留。sweeper：`healthy`。

### 本轮 50 条会话的 Token 与花费（按 2026-11-09 官方价格，北京时间 09:56 高峰时段）

| Run 类型 | 模型 | 成功调用 | Prompt Tokens | Cache Hit（命中率） | Cache Miss | Completion Tokens | 实际花费 | 无缓存原价 |
|---|---|---|---|---|---|---|---|---|
| `chat` | `deepseek-flash`（高峰） | 69 | 56,311 | 41,472（73.6%） | 14,839 | 731 | ¥0.037185 | ¥0.118470 |
| `chat` | `qwen3.7-flash` | 31 | 76,162 | 65,280（85.7%） | 10,882 | 183 | ¥0.004934 | ¥0.015379 |
| `open` | `deepseek-flash`（高峰） | 20 | 7,580 | 2,560（33.8%） | 5,020 | 280 | ¥0.012382 | ¥0.017400 |
| `open` | `qwen3.7-flash` | 30 | 12,149 | 3,072（25.3%） | 9,077 | 498 | ¥0.002337 | ¥0.002828 |
| **合计** | — | **150** | **152,202** | **112,384（73.8%）** | **39,818** | **1,692** | **¥0.056838（5.68 分）** | **¥0.154077（省 63.1%）** |

- **单请求总成本**：**¥0.001137 / 条**（`chat` ¥0.000842 + `open` ¥0.000294）。

主体延迟仍在约 3.5～8s；P95 被 **3 条离群**拉高（约 35s / 36s / 43s，联采与海纳都有），不是成片超时。

## 长尾根因（已查账本）

不是 `GATE_USER` / soft-busy 排队：那 3 条 chat 的 `queue` 只有约 **0.05～0.3s**，墙钟几乎全在 `exec`。

典型一条（43s，`session_id=0d4768e6…`）：

1. PLAN（`qwen3.7-flash`）成功 ~2.8s  
2. ANSWER（Qwen）失败：`PROVIDER_NETWORK`（`kind=network`，`retryable=true`，`response_json` 空）  
3. 中间空约 **36s**（worker 对可重试厂商错误做 outbox 退避，不是 HTTP 一直挂着）  
4. ANSWER 再失败一次同类错误，再 **fallback DeepSeek** 成功  

同窗（约 01:55–01:58 UTC）`model_calls` 汇总：Qwen 成功 61、失败 34（**全部** `PROVIDER_NETWORK`）；DeepSeek 成功 89、失败 **0**。没有 `PROVIDER_RATE_LIMITED` / `MODEL_CAPACITY_EXCEEDED`。失败延迟多在 **1.7～3s**，像连接/传输被掐断，不像读超时拖满。

结论：长尾 = **Qwen 瞬时网络类错误 + 退避重试**；最终靠 DeepSeek fallback 仍 100% 完成。不像本地并发闸门打满（否则 DeepSeek 也应一起吃紧，且会看到容量/429 码）。

## 结论

1. 多 org 冒烟链通：完成率 100%，无闸门 ACK 丢弃式卡死。
2. P50/P95 相对 S2 变差，是因为露出少量 Qwen `PROVIDER_NETWORK` 长尾，不是完成率或本地闸门问题；不要据此加 worker。
3. 实验 A 样本量已改为 **50 → 500 → 2000**；默认租户仍用 **S2（单 org 多用户）**，与 S3 对照分开记，不混进同一条曲线。

## 下一步

实验 A1：同 S2 租户分布，`--requests 50`，写正式基线记录。
