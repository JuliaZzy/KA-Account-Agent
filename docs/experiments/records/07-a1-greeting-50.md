# 实验：A1 — 运行链路基线（问候，单 org 多用户，50 条）

- **第几次**：实验 A 第 1 档（相对冒烟 S2 租户分布；样本量按计划 50 → 500 → 2000）
- **日期**：2026-11-09（UTC 2026-11-10T02:31Z 附近跑完）
- **代码**：含 S3 r3 的 Qwen async stream 修复；对照工作树
- **原始文件**：[`exports/load-test-greeting-a1-50.json`](../exports/load-test-greeting-a1-50.json)、[`exports/load-test-greeting-a1-50.log`](../exports/load-test-greeting-a1-50.log)

## 目的

冒烟 S1–S3 已通。A1 固定 **S2 租户分布**（单 org 多用户），用问候模板量运行链路基线（成功 P50/P95）。不改模板、并发、timeout。

本轮是真实模型压测，不是 `./dev test` / FakeLLM；消息仍是合成问候句，**不需要真实质量测试集**。

## 配置

| 项 | 值 |
|---|---|
| 命令 | `./dev py scripts/load_test_chat.py --base-url http://api:8000 --partnership-id ps-liancai-01 --user-id user-liancai-chen --user-ids user-liancai-chen,user-liancai-lin,user-liancai-zhou --requests 50 --concurrency 5 --output docs/experiments/exports/load-test-greeting-a1-50.json` |
| 消息模板 | `你好，这是第 {i} 条合成压测消息，请回复一句确认。` |
| 租户 | `ps-liancai-01` × 3 用户轮转（与 S2 相同） |
| Worker / title-worker | 5 / 2 |
| `GATE_USER` | 5 |
| HTTP / completion timeout | 30s / 180s |

## 结果

| 指标 | S2（20） | S3 r3（多 org 50） | **A1（50）** |
|---|---|---|---|
| 请求 / 成功 / 超时 | 20 / 20 / 0 | 50 / 50 / 0 | **50 / 50 / 0** |
| 完成率 | 100% | 100% | **100%** |
| 成功 P50 | 3.72s | 4.75s | **5.04s** |
| 成功 P95 | 6.77s | 8.79s | **76.60s** |
| 成功 max | — | 9.81s | **79.73s** |
| 成功 P99 | 未报 | 未报 | 未报 |

50 条里有 **5 条 ~76–80s**，其余主体在 ~5–7s（与 P50 一致）。长尾不是闸门打满。

## 调查

本轮 `model_calls`（按压测时间窗筛）：

| provider | status | cause | n |
|---|---|---|---|
| qwen | succeeded | — | 259 |
| qwen | failed | `APIConnectionError: Connection error.` | 7 |
| deepseek | failed | `APIConnectionError: Connection error.` | 6 |
| deepseek | succeeded | — | 2 |

worker 日志同一秒（`02:31:19`）五条 Qwen + 五条 DeepSeek 同时 `Connection error.`，随后走重试/退避才成功 —— 用户侧仍 50/50 完成。本轮无 `async_generator` / `Event loop is closed`，不是 S3 那类本地 async 组装 bug。

### 本轮 50 条会话的 Token 与花费（按 2026-11-09 官方价格）

| Run 类型 | 模型 | 成功调用 | Prompt Tokens | Cache Hit（命中率） | Cache Miss | Completion Tokens | 实际花费 | 无缓存原价 |
|---|---|---|---|---|---|---|---|---|
| `chat` | `qwen3.7-flash` | 100 | 136,732 | 100,096（73.2%） | 36,636 | 598 | ¥0.011809 | ¥0.027825 |
| `open` | `qwen3.7-flash` | 47 | 19,026 | 1,024（5.4%） | 18,002 | 831 | ¥0.004306 | ¥0.004470 |
| `open` | `deepseek-flash`（高峰） | 2 | 758 | 256（33.8%） | 502 | 28 | ¥0.001238 | ¥0.001740 |
| **合计** | — | **149** | **156,516** | **101,376（64.8%）** | **55,140** | **1,457** | **¥0.017354（1.74 分）** | **¥0.034035（省 49.0%）** |

- **单条请求成本（`chat` + `open`）**：**¥0.000347**（`chat` ¥0.000236 + `open` ¥0.000111；其中 2 次 `open` 因连接抖动降级到高峰期 `deepseek-flash` 略微抬高了标题成本）。

### 可能原因（已排查到哪一层）

「出站网络抖动」是对现有日志的归类，不是已经 DNS/connect/TTFB 分段测过的结论。按证据：

| 判断 | 依据 |
|---|---|
| **较像：共享出站路径闪断**（本机网络 / Docker 出站 / **VPN 切换**） | 两家厂商同秒一起 `APIConnectionError`；单家限流或单家路由解释不动 |
| 不太像：429 / 模型排队 / 首 token 慢 | 是硬连接失败，不是慢成功；错误码也不是 `PROVIDER_RATE_LIMITED` |
| 不太像：本地 async / event loop | cause 类型不对；同代码 r2 立刻干净 |
| 还分不清：Wi-Fi vs DNS vs TLS vs 死 keep-alive | 账本只记到包装后的 `Connection error.`，没有 `__cause__` / 分段计时 |

跑 A1 当时本机正在 **调整 VPN**（断连/重连或切换节点），与「所有对外请求同秒一起挂、稍后重试成功」高度吻合，**优先怀疑 VPN 切换打断了容器出站**，而不是加 worker 或改模型路由。

## 结论

1. 链通仍 100%（失败在单次模型调用，退避重试后都成功）；A1 的 P50 可用，**P95 被这次出站闪断污染，不能当基线**。
2. 无需改代码；同配置再跑一轮（A1 r2）拿干净 P95，再进 A2（500）。
3. 若 r2 在未动 VPN 时仍成批出现双边 `APIConnectionError`，再查容器出站 / DNS / 厂商侧；要钉死细类需补 `__cause__` 与分段计时。

## 已复测

同配置复测见 [`08-a1-r2-greeting-50.md`](08-a1-r2-greeting-50.md)：P50 4.92s / P95 11.80s / 无连接失败；**基线取 r2**。
