# 记录 31：Worker=12 + 联采三用户，打满 `GATE_ORG=10`

接记录 29、30。产品仍是刀 0–8；这一轮不改闸门默认、不加进程内线程。只改提供负载：聊天 worker `--concurrency=12`，客户端并发 18，联采三用户轮转。跑完已把 worker 收回 `--concurrency=5`。

- **目的**：让组织闸门在生产上限（`GATE_ORG=10`、`GATE_USER=5`）下真的打满。记录 29/30 卡在 Worker=5 和单用户 `GATE_USER=5`，组织峰值一直是 5。
- **命令**：`docs/experiments/golden_set/run_c_org_w12.sh`（题目与记录 26/29/30 相同：32 pre + 18 post；`--liancai-users user-liancai-chen,user-liancai-lin,user-liancai-zhou`；海纳题仍走 `user-haina-wu`）。跑的同时每 2 s 采 Redis `gate:*`。
- **原始文件**：[`c-org-w12-pre.jsonl`](../exports/c-org-w12-pre.jsonl)、[`c-org-w12-post.jsonl`](../exports/c-org-w12-post.jsonl)、[`c-org-w12-gates.tsv`](../exports/c-org-w12-gates.tsv)、[`c-org-w12-summary.json`](../exports/c-org-w12-summary.json)、pre/post 各一份 raw。
- **当时 worker**：聊天队列 `--concurrency=12`，`KA_ACCOUNT_POOL_SIZE=4`（连接预算：12×(4+2)=72，加上 title/sweeper/api 仍低于 97）。title-worker 仍是 3。闸门保持 `GATE_ORG=10`、`GATE_USER=5`、`GATE_MODEL=32`。

## 结果

| 组 | 吞吐（次/分） | 墙钟 | 出错 / 超时 | 轻任务 P50 | 轻任务排队 P50 / P95 | 轻任务最长 | 完整月报 P50 | 成本 |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| C 刀 5–8 Worker=5 单用户（记录 30） | 10.42 | 288 s | 0 / 0 | 15.98 s | 6.46 / 21.18 s | 129.8 s | 152 s | ¥1.238 |
| 本轮 Worker=12 三用户 | **15.23** | **197 s** | 0 / 0 | 17.46 s | 16.18 / 36.68 s | **48.5 s** | **128 s** | ¥1.154 |

50/50，0 客户端超时。用户：chen / lin / zhou 各 16，海纳吴 2（`t2s-10` ×2）。完整月报 6 次：90、118、128、132、134、148 s。

吞吐和月报中位都短了。轻任务 P50 几乎没动，排队中位反而从 6 s 升到 16 s：客户端 18 已经大于组织上限 10，多出来的短单要等座位。真正改善的是尾巴——记录 30 里 `po-01` 那种 130 s 卡在月报后面的情况，本轮最长只有 48 s。

## 闸门占用（Redis 605 行 / 94 个时间戳）

| 键 | 上限 | 峰值 | 打在峰值附近 |
|---|---:|---:|---|
| `gate:running:org:dist-liancai` | 10 | **10** | =10 占 9/94（10%）；≥8 占 24% |
| `gate:running:global` | 100 | **12** | =12 占 3/94（全体 worker 都在跑） |
| `gate:running:user:…:chen` | 5 | **5** | 3 个时间戳 |
| `gate:running:user:…:lin` | 5 | **5** | 2 个时间戳 |
| `gate:running:user:…:zhou` | 5 | 4 | 未顶满 |
| `gate:running:org:dist-haina` | 10 | 2 | `t2s-10` |
| `gate:model:qwen` | 32 | 12 | |
| `gate:model:deepseek` | 32 | 11 | |

**`GATE_ORG=10` 打满了。** 联采组织直方图：0–7 为主，8 有 7 个时间戳，9 有 7 个，10 有 9 个。全局峰值 12 = 联采 10 + 海纳 2，12 个 worker 座位用完。用户闸门两人顶到 5、一人到 4，不再是总开关。

`GATE_MODEL` 仍离 32 远（Qwen 12、DeepSeek 11）。多出来的并发来自 12 个 worker 领不同 Run，不是再往同一个进程里加线程。

父 Run `run.enqueued` > 1 仍是派波叫醒，和记录 29 一样。data/tool 子 Run 里各有 3 次多一次入队，和组织闸门打满后的 `SoftBusy` 重试相符；worker 在采集结束后已重建成 5，日志里的 `gate_full` 行没有留下来。

## 检索入波

第一波仍是 5–7 条 data + 2–3 条 search。检索相对 q1：5.4–15.0 s（中位 10.4 s），和仍在跑的 data 重叠 1–12 s。比记录 30 的中位 2.4 s **更晚**开工：同一时刻有多份月报在抢 10 个组织座位，`s*` 仍排在 `q*` 后面。Worker 加到 12 并不能让检索和第一批 data 同时开工，只是让更多月报叠在一起。

## 结论

1. **记录 26「Worker 5→10 没收益」是单用户 `GATE_USER=5` 绑死的。** 三用户 + Worker=12 之后，组织闸门到 10，吞吐 10.4 → 15.2 次/分，月报中位 152 → 128 s，轻任务最长 130 → 48 s。
2. **现在卡住的是 `GATE_ORG=10`，不是 Worker=12。** 全局偶尔到 12，是海纳那 2 个座位补上的。再加 worker 而闸门不变，联采侧不会超过 10。
3. 客户端并发 18 > 10，所以短单排队变长是预期，不是回退。
4. `GATE_USER=5` / `GATE_ORG=10` 不必改成 30/50；按生产上限就能测到并发。compose 默认 worker 仍是 5。

## 遗留

1. 检索不要全部排在 `q*` 后面（记录 28），本轮 Worker=12 仍没让 `s*` 和第一批 data 同时开工。
2. Qwen 审阅、空月饼图诚实闸，仍未动。
