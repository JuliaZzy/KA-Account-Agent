# 第 8 条：成本、性能与固定测试框架

本文定义每次版本更新后都要执行的测试层次、测试文件、指标和实验记录。它不把监控阈值当作实测结果；只有保存原始输出后，结果才可以回填到本文或简历。

## 1. 测试目标

固定验证以下指标：

- 质量不下降：关键回归用例、业务规则、安全边界和幂等行为；
- Token：Prompt、Completion、Total Token，以及每个 Run/步骤的 Token；
- 成本：单 Run、按 provider/model、每日总成本和预算拦截；
- 延迟：HTTP 入队、排队等待、模型调用、工具调用、Run 完成时间的 P50/P95/P99；
- 吞吐：稳定并发、每分钟完成 Run 数、队列积压和失败率；
- 稳定性：429、5xx、超时、重试、卡住 Run 和重复副作用拦截。

质量指标、成本指标和性能指标分开报告。不能用“更快”抵消业务数字错误，也不能用“回答正确”掩盖成本或超时问题。

## 2. 固定测试层次

### 2.1 代码级和领域回归

这些测试不请求真实模型，使用 `FakeLLM` 或确定性 Fixture：

- `tests/test_rules.py`：采购价格、MOQ、折扣和商业规则的确定性计算；
- `tests/test_slots.py`：采购槽位填充、缺字段和状态推进；
- `tests/test_check.py`：草稿校验和规则拒绝；
- `tests/test_submit.py::test_confirm_creates_one_order_and_burns_the_token`：确认 token 先消费、只创建一张订单；
- `tests/test_contract_read.py::test_read_and_file_an_agreement`：合同读取、抽取和确认入库；
- `tests/test_report_agent.py::test_review_passes_at_85_and_revises_below_it_for_three_rounds`：报表审核低于阈值时修订；
- `tests/test_report_agent.py::test_insight_code_reads_the_rows_and_rejects_an_import`：分析代码读取数据并拒绝危险导入；
- `tests/test_brand_rag.py::test_brand_question_is_retrieved_then_answered_by_the_text_model`：检索后生成带来源回答；
- `tests/test_text2sql.py::test_a_question_only_sees_this_partnership`：合作关系数据范围隔离；
- `tests/test_text2sql.py::test_a_write_is_refused_and_the_order_stays`：写操作拒绝且业务数据不变。

### 2.2 编排、并发和恢复

- `tests/test_fanout.py::test_same_wave_is_two_child_runs`：无依赖任务进入同一波并行执行；
- `tests/test_fanout.py::test_sweep_wakes_a_parent_after_children_finish`：子任务结束后唤醒父 Run；
- `tests/test_fanout.py::test_task_wave_reuses_an_existing_child_without_a_second_outbox`：恢复时不重复创建子 Run/outbox；
- `tests/test_run_api.py::test_loop_runs_same_turn_tools_in_parallel`：同一波工具并行；
- `tests/test_run_api.py::test_a_worker_continues_from_a_tool_result_without_calling_it`：checkpoint 恢复不重复调用已完成工具；
- `tests/test_session_concurrency.py::test_two_sessions_overlap_on_the_model`：不同会话可以并发执行；
- `tests/test_session_concurrency.py::test_one_session_finishes_a_turn_before_the_next_starts`：同一会话保持写入顺序；
- `tests/test_orchestrator_contract.py::test_unknown_agent_is_dropped_and_a_dangling_dependency_is_rejected`：未知 agent 和悬空依赖拒绝；
- `tests/test_orchestrator_contract.py::test_a_report_drops_its_data_task_and_the_dependency`：报表任务不会重复排入内部查数任务；
- `tests/test_orchestrator_contract.py::test_a_cycle_is_rejected`、`::test_a_cycle_fails_the_run_without_children`：编排计划成环检测；
- `tests/test_orchestrator_contract.py::test_answer_materials_keep_failed_and_skipped_results`：ANSWER 保留失败和跳过任务结果；
- `tests/test_orchestrator_contract.py::test_an_unknown_agent_is_counted_when_the_plan_still_succeeds`：计划部分成功时仍记录未知 agent；
- `tests/test_orchestrator_contract.py::test_a_second_result_keeps_the_first_and_a_lost_checkpoint_does_not_replan`：重复结果保留第一次写入，checkpoint 丢失时不重复规划；
- `tests/test_execution_contract.py::test_drain_and_production_finish_the_same_turn`：测试 drain helper 和生产路径完成同一轮时使用同一账本语义；
- `tests/test_execution_contract.py::test_a_lost_worker_resumes_without_sending_twice`：Worker 丢失后恢复，副作用不重复发送。

### 2.3 模型、工具和容量控制

- `tests/test_llm.py::test_vendor_usage_is_actual_and_a_missing_usage_is_estimated`：区分厂商实际 Token 和本地估算，同时验证缓存字段写入 model call 和 usage record；
- `tests/test_llm.py::test_usage_keeps_deepseek_and_qwen_cache_fields`：保留 DeepSeek/Qwen 的缓存命中字段，并规范成统一的缓存 usage 字段；
- `tests/test_llm.py::test_a_run_over_its_model_budget_does_not_call`：Run 达到调用预算后不再请求模型；
- `tests/test_llm.py::test_an_org_over_its_daily_tokens_does_not_call`：组织每日 Token 预算拦截；
- `tests/test_run_api.py::test_model_capacity_requeues_without_spending_execution_attempt`：本地模型容量不足回队列且不消耗业务执行次数；
- `tests/test_run_api.py::test_provider_rate_limit_requeues_with_retry_after`：供应商限流按 Retry-After 回队列；
- `tests/test_entry_guard.py`：Run、模型和工具并发 Lease 的占用、续期、释放和过期回收；
- `tests/test_provider_errors.py`：429、超时、网络错误、熔断和不可重试错误的分类。

缓存字段只做观测，不在这一层开启显式缓存。`cache_hit_tokens`、`cache_miss_tokens`、
`cache_creation_tokens` 是可空列；供应商没有返回缓存细分时保持 `null`，供应商明确返回 0 时保存为 0。
DeepSeek 的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 和 Qwen 的
`cached_tokens`（包括 `prompt_tokens_details` / `input_tokens_details` 下的形式）都映射到这些字段。
`total_tokens` 仍是原始输入加输出总量，不能拿它单独推导实际账单。

当前只观察 DeepSeek 和 Qwen 的供应商缓存结果。GLM 不在本轮缓存优化范围内。

### 2.4 模型路由和降级回归

- `tests/test_model_routing.py::test_plan_policy_has_primary_fallback_and_budget_metadata`：验证 PLAN 的首选模型、备用模型、策略版本和预算元数据；
- `tests/test_model_routing.py::test_unknown_quality_gate_fails_closed`：验证未知或拼写错误的质量门槛不会静默放行，并返回结构化失败原因；
- `tests/test_model_routing.py::test_task_plan_quality_check_handles_a_deep_dependency_chain_iteratively`：验证 1200 层依赖链使用迭代 Kahn 算法，不触发递归深度限制；
- `tests/test_model_routing.py::test_unknown_purpose_keeps_the_requested_model`：验证没有单独配置的 purpose 仍用调用方原来的模型作为首选；
- `tests/test_model_routing.py::test_blank_purpose_does_not_select_a_route`：验证未传 purpose 时不进入路由；
- `tests/test_model_routing.py::test_named_purposes_receive_a_compatible_route`：验证带 purpose 的领域调用都进入兼容路由；
- `tests/test_model_routing.py::test_plan_provider_error_uses_the_configured_fallback`：验证首选模型发生 ProviderError 时切换到备用模型；
- `tests/test_model_routing.py::test_route_events_are_written_to_the_existing_trace`：验证路由选择事件写入已有 Run trace，包含 trace_id 和选中的模型；
- `tests/test_model_routing.py::test_quality_gate_failure_falls_back_and_is_visible_in_trace`：验证 PLAN 质量门槛失败后触发 fallback，并记录质量失败和降级事件；
- `tests/test_model_routing.py::test_quality_gate_failure_falls_back_after_model_completed`：验证真实 `model.completed` 记账后，质量门槛失败仍能降级，不把失败调用算进 `max_model_calls`；
- `tests/test_chat_run.py::test_enqueue_writes_open_run_budget`：验证建 OPEN Run 时写入安全网预算。

这组测试不直接测吞吐或 P95，但属于固定版本回归的一部分。错误的模型路由会改变每个请求的 Token、成本、延迟和质量，因此在性能实验前必须先通过。当前 trace 测试使用轻量账本替身验证事件结构；接入完整数据库 Fixture 后，再增加 HTTP `/api/runs/{run_id}/trace` 的端到端断言。

### 2.5 运行指标和监控接口

这些测试不调真实模型，也不产生第一次实验记录。

- `tests/test_run_api.py::test_the_monitor_lists_one_org_and_reads_the_ledger`：监控只能读取当前组织，并从运行账本生成指标；
- `tests/test_run_api.py` 中 `/metrics` 相关测试：Prometheus 文本和敏感字段过滤；
- `tests/test_performance_metrics.py`：压测 JSON 的百分位算法。20 条成功加 1 条 180 秒超时 → 报 P50 和 P95，P99 留空，成功 P95 不被超时抬高。300 条成功加 1 条超时 → 报 P95 和 P99，P99 不是最慢的那一条。耗时是写进 JSON 的假数字；
- `tests/test_load_test_chat.py::test_timeout_preserves_run_id_and_server_status`：验证超时保留 Run ID 和当时服务端状态；
- `tests/test_load_test_chat.py::test_enqueue_errors_are_not_counted_as_client_timeouts`：验证入队错误不归类为客户端超时；
- `tests/test_load_test_chat.py::test_follow_up_is_bounded_and_does_not_reclassify_a_client_timeout`：验证追踪有界且保留客户端超时结果；
- `tests/test_load_test_chat.py::test_follow_up_records_later_failure_and_related_runs`：验证后续服务端失败和关联 Run 状态分别记录；
- `tests/test_load_test_chat.py::test_session_run_facts_are_scoped_to_experiment_sessions`：验证数据库查询只包含指定实验会话；
- `tests/test_load_test_chat.py::test_open_loop_reports_generator_drops_separately`：验证开环生成器丢弃不算服务端失败；
- `tests/test_performance_metrics.py::test_zero_timeouts_are_not_replaced_by_other_failures`：验证汇总不会把零超时覆盖为其他失败数；
- `tests/test_monitor_metrics.py`：空样本百分位是 `null`；P50 ≤ P95 ≤ P99；完成时间只含成功 run；失败和仍在排队的 run 不进完成时间样本；直方图有 180s 和 300s 桶，模型桶更短；`/metrics` 文本没有 `run_id` / `session_id` / `user_id`；
- `backend/services/monitor.py`：账本上的 P50/P95/P99、阶段耗时、PLAN 错误率和 SLO 告警。

运行监控给出 P50、P95 和 P99，空样本是 `null`。压测里成功样本不足 300 只报 P50 和 P95；300 条及以上才报三档，这是报告门槛，不保证百分位稳定。失败和超时不进入成功样本的百分位。`started_at - created_at` 是**首次排队**，不包含重试或等待子任务后的再次排队；`ended_at - started_at` 是首次启动到终态的墙上时间，可能包含重排队和等待，不是纯执行时间。模型 / 工具来自各自的 `latency_ms`。累计排队时间暂未实现，不能把首次排队冒充总排队。

超时、并发、租约和 SLO 的默认值在 [`../architecture/architecture.md`](../architecture/architecture.md) 的「当前运行标准」。

## 3. 实验怎么跑

本阶段每组实验只跑一次，暂不执行交替 A/B；单轮结果仅描述当次环境，不能单独证明版本造成的性能提升。历史实验保留原始结果和原有口径，新脚本结果单独记录，不覆盖历史文件。

脚本现在保存每个请求的 `run_id` 和客户端 `outcome`：`completed`、`client_timeout`、`enqueue_error`、`poll_error`；开环发不出去的请求记 `generator_dropped`。客户端超时不会取消服务端任务。发完后按本次 session 范围追踪 chat、OPEN 和相关 Run，默认在 30 秒内停止追加轮询（`--settle-timeout`，可设 0 立即取快照；单次数据库连接或查询本身的耗时不受此参数约束），记录追踪结束时的状态和仍未完成数。队列为空不代表任务已结束；不要自动取消或等待整个共享环境排空。

`timeouts` 只数客户端 completion timeout；失败和生成器丢弃分别报告。成功 P50/P95/P99 只包含客户端期限内收到回复的请求，超时是截尾样本，其真实完成时间不能当作超时阈值。费用在追踪快照后汇总，当前只包括账本中成功的模型调用；仍有 Run 未终态或存在未记录 usage 的失败调用时可能低估，见 `snapshot_incomplete` 和 `scope_note`。

单测锁住算法和契约。第一次实验记录来自下面的压测，调真实模型。顺序固定：

1. 对照官网把各模型 RPM/TPM 填进架构文档「当前运行标准」，再决定 worker 和 `--concurrency`，不要先写成 5 或 10；
2. `./dev lint` 和 `./dev test`（**不调真实模型**，见下）；
3. 用固定消息模板跑第一轮压测，保存 JSON、提交号、worker 并发和模型配置；
4. 把数字填进第 7 节模板，标成「第 1 次」；
5. 按第 6 节的调查顺序看未完成和过慢的请求；
6. 改一处明确瓶颈后再跑第二轮，模板、并发、timeout、**租户分布**不变，标成「第 2 次」。

### `./dev test` 测什么

`./dev test` = Compose 容器里跑 `pytest`（见仓库根 `dev`）。连的是 compose 里的 Postgres 一次性库，模型一律 `FakeLLM` / Fixture，**不联网、不花 token**。覆盖计划 §2.1–2.5：商业规则、编排/并发、闸门、路由、指标算法等。它**不是**端到端冒烟，也不是实验 A 的延迟基线。

前端单测另走 `./dev test-web`。汇总 JUnit 用 `scripts/resume_metrics.py`：

```bash
./dev test --junitxml=.resume-eval.xml 2>&1 | tee docs/experiments/exports/pytest-full.log
./dev py scripts/resume_metrics.py --junitxml .resume-eval.xml \
  --output docs/experiments/exports/resume-metrics.json \
  2>&1 | tee docs/experiments/exports/resume-metrics.log
```

### 样本量阶梯（问候路径）

不要把单测里的假耗时当成系统实测。压测成功样本不足 **300** 时脚本**不报 P99**（只报成功 P50/P95）；超时不进成功百分位。

| 阶段 | 条数 | 目的 | 租户 |
|---|---|---|---|
| 冒烟 S1 | 10 | 链通（入队 → worker → 模型 → assistant） | 单用户 |
| 冒烟 S2 | 20 | 打到 `GATE_USER` 之外：同 org 多用户 | 单 org、多用户 |
| 冒烟 S3 | 50 | 打到 `GATE_ORG` / 全局：多 org | 多 org、多用户 |
| 冒烟 S4 | 30 × 3 轮 | 首批冷启动：worker 重启后，单用户 vs 三用户（总条数相同），并看前缀缓存冷热 | 单用户 / 单 org 三用户 |
| 实验 A1 | 50 | 正式基线，报成功 P50/P95 | **固定一种**租户分布（见下） |
| 实验 A2 | 500 | 同一分布，成功 ≥300 后报 P99 | 与 A1 相同 |
| 实验 A3 | 2000 | 同一分布，增加 P99 尾部样本；成本最高 | 与 A1 相同 |

设计取舍：

- **冒烟用租户阶梯，实验 A 用样本量阶梯**。冒烟要暴露闸门/隔离问题；基线要可比，所以 A1/A2/A3 只改条数，不改模板、并发、timeout、租户分布。
- 单用户压测**不够真实**：第 1 次 10 条冒烟卡在同一 `GATE_USER` 上就是例子。S2/S3 是必要的，不是锦上添花。
- A 的默认租户建议用 **S2（单 org 多用户）**：比单用户更接近真实，又比多 org 便宜、好解释。若主要怕 org 闸门，再单开一组「多 org」对照，不要和 A 的 50/500/2000 混在一张表里比。
- 50 → 500 → 2000：50 看 P50/P95；500 过 P99 报告门槛（成功 ≥300）并留超时余量；2000 增加尾部样本，不保证跨轮稳定。中间不再插 100。
- `load_test_chat.py` 支持 `--user-ids`（同 org 多用户）和配对的 `--partnership-ids` + `--user-ids`（多 org）。种子：联采 `user-liancai-*` / `ps-liancai-*`，海纳 `user-haina-*` / `ps-haina-*`。

问候路径 PLAN/ANSWER 都走官方 Qwen（`qwen3.7-flash`）。压测并发默认 5，不要超过 `min(worker, GATE_ORG, GATE_USER, GATE_MODEL, 官网 RPM)`。

### 缓存观测（实验 C 用）

缓存命中的原理是服务端复用重复输入前缀的 KV 状态；请求仍然可以携带完整输入，
但命中部分单独计量，通常按低于普通输入的价格计费。DeepSeek 官方 API 默认启用上下文缓存，
Qwen 对支持的模型自动尝试隐式 Context Cache。两者都只保证前缀匹配条件，不保证每次命中。

- DeepSeek：关注 `prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`；
- Qwen：关注 `cached_tokens` 和显式缓存场景下的 `cache_creation_input_tokens`；
- 统计命中率时按 provider/model 分组，使用 `cache_hit_tokens / prompt_tokens`，只对缓存字段非空的样本计算；
- 统计创建 token，避免把显式缓存的创建成本误当成命中收益；
- 先记录真实命中率、命中 token、TTFT/模型延迟和账单，再决定是否重排 prompt 或增加显式 `cache_control`；
- 不把“缓存字段为空”当成缓存未命中，它表示供应商没有提供该细分字段。

官方计费和限制会变化，实验记录应保存对应日期的文档版本：

- [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/) 和 [DeepSeek Pricing](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)；
- [Qwen 模型价格](https://help.aliyun.com/zh/model-studio/model-pricing) 和 [Qwen Context Cache](https://help.aliyun.com/zh/model-studio/context-cache)。

### 当前模型单价表（核对日期：2026-11-09，人民币 / 百万 Tokens）

代码实现见 `backend/llm/pricing.py`，入库与监控汇总在 `backend/db/database/records.py` 与 `backend/db/database/run.py`：

| 模型 | 供应商 / 地域 | 计费时段 / 阶梯 | 输入（缓存未命中） | 输入（缓存命中） | 显式缓存创建 | 输出（含思维链） |
|---|---|---|---|---|---|---|
| `qwen3.7-flash` | 阿里云百炼（华北2北京） | `0 < prompt ≤ 32K` | **¥0.20** | **¥0.04**（隐式 20%）/ **¥0.02**（显式 10%） | **¥0.25**（125%） | **¥0.80** |
| `qwen3.7-flash` | 阿里云百炼（华北2北京） | `32K < prompt ≤ 256K` | ¥0.60 | ¥0.12（隐式 20%）/ ¥0.06（显式 10%） | ¥0.75（125%） | ¥2.40 |
| `qwen3.7-flash` | 阿里云百炼（华北2北京） | `256K < prompt ≤ 1M` | ¥1.20 | ¥0.24（隐式 20%）/ ¥0.12（显式 10%） | ¥1.50（125%） | ¥4.80 |
| `deepseek-flash` | DeepSeek 官方（`V4.1-Flash-1017`） | **空闲时段**（工作日其余时间 + 周末/节假日全天） | **¥1.00** | **¥0.02** | — | **¥4.00** |
| `deepseek-flash` | DeepSeek 官方（`V4.1-Flash-1017`） | **高峰时段**（北京时间周一至周五 09:00–12:00、14:00–18:00） | **¥2.00** | **¥0.04** | — | **¥8.00** |

隐式缓存通常不需要应用侧配置；显式缓存需要稳定且足够长的前缀，并有创建成本和有效期。
在没有命中数据之前，不把显式缓存写进通用调用层，也不把固定 system prompt 默认标记为可缓存。

## 4. 固定执行顺序

每次版本更新按以下顺序执行：

1. `./dev lint`；
2. `./dev test`（FakeLLM，不调真实模型），确认代码和领域回归；
3. 生成 JUnit XML，用 `resume_metrics.py --junitxml ... --output ...` 汇总执行用例的通过率；
4. 模型路由、降级和离线百分位算法已含在全量 pytest 里，不必单独再跑一遍，除非本轮只改了那一块；
5. 启动 Compose（api / worker / title-worker / sweeper healthy）；
6. **端到端冒烟租户阶梯** S1 → S2 → S3（真实模型；任一步完成率明显掉再停，先查闸门/会话锁/sweeper）；
7. **实验 A** 固定租户分布后跑 50 → 500 → 2000；
8. 读 Run ledger 和 `/monitor`，核对 Token、成本、队列、模型指标；
9. 原始 JSON + 控制台 log 进 `docs/experiments/exports/`，结论进 `docs/experiments/records/NN-*.md`（NN 是实验顺序，见 [`experiments/README.md`](../experiments/README.md)）。

产物路径：

| 产物 | 位置 |
|---|---|
| 压测 JSON | `docs/experiments/exports/<name>.json`（`--output`，勿写仓库根） |
| 控制台 log | 同名 `.log`（`2>&1 \| tee`） |
| 实验说明 | `docs/experiments/records/<NN>-<id>-<name>.md`，NN 接着上一份往后编（01、02……），同一实验复测加 `-r2`、`-r3` 但仍占新的序号 |
| JUnit / 回归汇总 | `.resume-eval.xml` → `docs/experiments/exports/resume-metrics.json` |

命令入口（单用户冒烟 S1；S2/S3 待脚本支持多用户后换用户列表）：

```bash
./dev lint 2>&1 | tee docs/experiments/exports/lint.log
./dev test --junitxml=.resume-eval.xml 2>&1 | tee docs/experiments/exports/pytest-full.log
./dev py scripts/resume_metrics.py --junitxml .resume-eval.xml \
  --output docs/experiments/exports/resume-metrics.json \
  2>&1 | tee docs/experiments/exports/resume-metrics.log

OUT=docs/experiments/exports/load-test-smoke-s1-10
./dev py scripts/load_test_chat.py \
  --base-url http://api:8000 \
  --partnership-id ps-liancai-01 \
  --user-id user-liancai-chen \
  --requests 10 \
  --concurrency 5 \
  --output ${OUT}.json 2>&1 | tee ${OUT}.log
```

实验 A 把 `--requests` 换成 50 / 500 / 2000，`--output` 换成 `load-test-greeting-a1-50` 等；模板、并发、timeout、租户分布保持不变。不同业务路径（实验 B）分开跑，不要把多条路径的样本拼成一个 P99。

## 5. 第 8 条实验计划

### 执行顺序（A 之后，2026-11-10 调整）

字母只是实验编号，不是执行顺序。A 之后按下面的顺序，让每一轮的数据被后面复用：

1. **B1：golden set 一遍（并发 1）**：同时得到质量基线、无竞争延迟，以及 C 需要的优化前 Token/成本。**已完成**（记录 14）。
2. **B2：并发 5 压测**：用 golden 里几条代表性问题当模板。这一轮同时是 D 的 Worker=5 数据点。
3. **D：补 Worker 3 和 10 两轮**：只改 Worker 数，其余与 B2 完全一致。
4. **C：做 Token/上下文优化**：每次只改一处，再跑一遍 golden set（并发 1），与 B1 对比；质量不得低于 B1 的阈值。
5. **E：最后单独跑**：故障注入会污染延迟和完成率，所以不与上面混跑。

复用的前提：B2/D 的模板、租户分布、timeout 固定；C 对比必须同一份 `golden_v1.jsonl`、并发 1、同样的判分口径（B1 记录里的修订口径）。

### 实验 A：运行链路基线

目的：测量 API、outbox、Redis、Celery、Postgres、Worker 和 SSE/API 周边的基础耗时。

- 输入：简单问候模板（与冒烟相同）；
- 请求数：**50 → 500 → 2000**（A1 报 P50/P95；A2 起可报 P99；A3 增加尾部样本）；
- 租户：冒烟 S1–S3 通过后，A 默认固定为 **单 org 多用户**；需要 org 闸门数据时另开对照，不塞进同一条样本量曲线；
- 并发：worker 5；不要靠加并发抬完成率，先查 soft-busy ACK、会话锁和 sweeper 补投；
- 指标：完成率、超时数、成功 P50/P95（够样本再加 P99）、队列等待、模型调用、Token 和成本。

### 实验 B：领域路径基线

目的：分别测量 Text2SQL、品牌检索、报表和采购路径，定位模型、检索、数据库或工具耗时。分两步：B1 = golden set 并发 1（已完成，记录 14）；B2 = 并发 5 压测，同时作为 D 的 Worker=5 数据点。

- 每个场景先 20 条看 P50 和 P95；
- 要报该场景 P99 时，成功样本凑到 300，不要 50～200 就写 P99；
- 采购确认、发信和 MCP 副作用不直接压测真实外部系统，使用 Fake 或 dry-run 适配器；
- 同时检查质量回归和副作用重复拦截。
- B1 历史合同导入没有模型账本，成本不可回填；新一轮合同导入记录独立 run，`run_golden.py` 保存 OCR 与抽取账本，`summarize_golden.py` 的 `contract_import` 单列导入耗时与费用，不与聊天路径混算。汇总中的 `missing_ledger_samples`、`legacy_ledger_samples` 和 `unknown_usage_calls` 非零时，已知费用只是下限，不能当完整成本与历史结果直接比较。

### 实验 C0：text2sql 选型（2026-11 完成）

不是原计划里的实验，是做 C 之前需要先定的事：text2sql 用旧 workflow 还是 ReAct。结论是只保留 ReAct，旧 workflow 不再对比。过程和修复见记录 15 到 23：
workflow 对 ReAct（16）、难题开发集（17、18）、留出集（19）、全量金标回归（20、22、23）和修复（21）。
ReAct 的优势集中在空结果重查和报错修复，成本约高 37% 到 45%。C 的优化前基线用记录 23 的最终回归。评测集合并为 `golden_all_v2.jsonl`（`split` = core / dev / heldout），用法见 `merge_golden_v2.py`。

### 实验 C：上下文和 Token 优化

比较历史消息裁剪、长期记忆摘要、工具 Schema 精简和模型调用预算变化。

- 固定同一批质量评测样本：即 golden set（`golden_v1.jsonl`，并发 1），优化前基线直接取 B1；
- 记录平均、P50 和 P95 的 Prompt Token、Completion Token、单 Run 成本；
- 记录可用样本的缓存命中 token、未命中 token、缓存创建 token 和命中率，并按 provider/model 分开；
- 记录命中率与模型调用延迟的变化，不能只看 `prompt_tokens` 是否减少；
- 质量下降超过预先设定阈值时，优化不通过。

### 实验 D：并行和队列优化

比较单波串行、依赖分波和多个 Worker 并发下的完成时间、队列等待、吞吐和失败率。

- 使用 `tests/test_fanout.py` 的多任务场景；
- 使用 3、5、10 个 Worker 并发配置做独立实验；其中 Worker=5 直接取 B2，只需补 3 和 10 两轮；
- 关注模型 provider 并发上限，不能只增加 Worker 数量。

现有 `--concurrency` 是闭环并发：一个请求完成或客户端超时后才放行下一个，适合测固定并发下的表现。研究固定到达率时另用开环模式，例如 `--arrival-rate 30 --max-pending 20` 表示计划每分钟到达 30 条，生成器内在途和等待总数最多 20。记录计划到达时间、实际发送时间、生成器延迟、offered、submitted、generator_dropped、完成速率和服务端队列状态。`open_loop_send_rate_per_minute` 按首末实际发送间隔计算；完成速率按从调度开始到所有已发送请求返回的时间计算。生成器丢弃不算服务端失败；请求耗时从实际发送开始，需同时观察生成器延迟。先从低速小样本开始，不与闭环基线直接混算。

### 实验 E：限流和故障恢复

验证模型 429、超时、Worker 中断、过期 Lease、stale outbox 和 response_lost 的行为。

- 复用 `tests/test_provider_errors.py`、`tests/test_run_api.py` 的重试和清扫测试；
- 记录恢复时间、重试次数、业务执行次数和副作用重复执行次数；
- 任何副作用在 response_lost 后都不得自动重复调用。

## 6. 40 条请求、并发 5、完成率 62.5% 的调查记录

这是历史摸底，不是第 1 次正式实验。来自 `scripts/load_test_chat.py`：40 条请求、并发 5，25 条完成，完成率 62.5%，15 条在 180 秒 completion timeout 内没有完成。观测 P95 为 180.30 秒（被超时截断），成功请求 P50 为 7.76 秒、P95 为 99.73 秒。样本不够，没有 P99。

这不能直接归因于某一个组件，也不能当作性能成果。现有数据只能说明：部分请求很快完成，但尾部请求持续排队、重试或未能在超时前写回 assistant message。

调查顺序固定为：

1. 保存每个请求的 `session_id`、`run_id`、提交时间、完成时间和错误字段；
2. 对未完成请求查询 `/api/runs/{id}/trace` 和数据库 `agent_runs`；
3. 区分 `queued`、`running`、`waiting_for_tasks`、`waiting_for_user`、`failed` 和终态；
4. 查看 `queue_lag`、`model_calls.latency_ms`、`tool_calls.latency_ms`、429、超时和 `retry_after_seconds`；
5. 查看 Worker 日志和 `pending_jobs`、`active_runs`、`stuck_runs`；
6. 检查是否在同一会话中提交了多条消息、是否受到会话锁或 provider 并发限制；
7. 修复一个明确瓶颈后，用相同模板、相同并发、相同租户分布复测。先看 P50/P95；要写 P99 用 A2/A3（500 / 2000，成功 ≥300）。

可能原因包括模型 provider 限流、模型槽位不足、Worker 并发与 provider 限额不匹配、队列等待、Run 重试、会话锁、SSE 轮询观察误差或 completion timeout 偏短。必须通过 trace 和日志确认，不能凭完成率猜测。

## 7. 实验结果记录模板

正式实验写在 [`docs/experiments/records/`](../experiments/records/)，一份实验一个 Markdown，文件名前缀是实验顺序号（索引见 [`experiments/README.md`](../experiments/README.md)）。下面是字段清单；填完后不要只留在聊天里。

```text
实验名称：
第几次：第 1 次 / 第 2 次
代码提交：
工作区是否有未提交改动：
实验开始/结束时间及所在时区：
机器 CPU / 内存 / 操作系统 / Docker Desktop：
Provider 区域、端点类型和模型快照：
预热请求数、是否剔除预热数据：
并发运行的其他工作及实验前后相关 Run 状态：
缓存预热情况：
数据集/消息模板：
负载模式：闭环 / 开环；计划及实际发送速率：
生成器最大待处理数及丢弃数：
请求数 / 成功数 / 超时数：
压测并发：
Worker `--concurrency`：
`KA_ACCOUNT_GATE_USER` / `KA_ACCOUNT_GATE_MODEL`：
官网 RPM/TPM（对照日期和文档链接）：
模型/provider：
HTTP timeout / completion timeout：
完成率：
超时与失败原因：
成功 P50：
成功 P95：
成功 P99（成功样本 < 300 填「未报」）：
Prompt Token：
Completion Token：
Cache Hit Token：
Cache Miss Token：
Cache Creation Token：
Cache Hit Ratio（仅统计有缓存字段的样本）：
单 Run 成本：
队列等待：
模型调用延迟：
工具调用延迟：
吞吐：
质量回归：
结论和下一步：
原始文件：
```

已记录：

- [`01-s1-smoke-concurrency-5.md`](../experiments/records/01-s1-smoke-concurrency-5.md)（S1 单用户 10，第 1 次 8/10）
- [`02-s1-r2-smoke-concurrency-5.md`](../experiments/records/02-s1-r2-smoke-concurrency-5.md)（同模板修复后 10/10）
- [`03-s2-multiuser-20.md`](../experiments/records/03-s2-multiuser-20.md)（S2 单 org 多用户 20，20/20）
- [`04-s3-multiorg-50.md`](../experiments/records/04-s3-multiorg-50.md)（S3 多 org 50，50/50；P95 有 3 条长尾）
- [`05-s3-r2-multiorg-50.md`](../experiments/records/05-s3-r2-multiorg-50.md)（S3 r2：抓到 `TypeError: async_generator`；P95 11.3s）
- [`06-s3-r3-multiorg-50.md`](../experiments/records/06-s3-r3-multiorg-50.md)（S3 r3：修 async stream；P95 8.8s）
- [`07-a1-greeting-50.md`](../experiments/records/07-a1-greeting-50.md)（A1 第 1 次：出站抖动污染 P95 76s）
- [`08-a1-r2-greeting-50.md`](../experiments/records/08-a1-r2-greeting-50.md)（A1 基线：P50 4.9s / P95 11.8s）

- [`09-a2-greeting-500.md`](../experiments/records/09-a2-greeting-500.md)（A2：500/500；P50 4.9s / P95 6.5s / P99 7.2s；账本零失败）
- [`10-s4-cold-start-30.md`](../experiments/records/10-s4-cold-start-30.md)（S4：worker 冷启动第一波约 +3.5s，与用户数、前缀缓存冷热无关；缓存冷只多花约 15%）

- [`11-a3-greeting-2000.md`](../experiments/records/11-a3-greeting-2000.md)（A3：1998/2000；P50 4.6s / P95 6.0s / P99 7.4s；2 条因瞬时 DB 连接尖峰永久 queued；总花费 ¥0.574）

- [`12-a3-r2-greeting-2000.md`](../experiments/records/12-a3-r2-greeting-2000.md)（A3 r2：修复后复测，对话 2000/2000、最长 19s、连接泄漏 27→5；但暴露补投把「在排队」当成「丢了」：标题队列积压触发 3083 次补投、误杀 24 个 Run）
- [`13-a3-r3-greeting-2000.md`](../experiments/records/13-a3-r3-greeting-2000.md)（A3 r3：补投先看队列深度 + title-worker 并发 3；4000 个 Run 全成功、补投 0 次、死信 0；P50 5.0s / P95 7.6s / P99 9.7s，比 r1 慢约 8% 原因未区分；单条 ¥0.000303）
- [`14-b-domain-baseline-golden.md`](../experiments/records/14-b-domain-baseline-golden.md)（B1：39 条 golden set × 3 次；质量 67%（原版口径 51%）；Text2SQL P50 12.8s / P95 17.9s、约 95% 是串行模型调用；月报 66s / 31 次调用 / ¥0.018；报表 0/21，发现图表谎报、SKU 歧义、`ROUND` 类型错误、月份口径不一致）

下一步：按 §5「执行顺序」——B2（并发 5）→ D（Worker 3/10）→ C → E。B1 已完成（记录 14），其发现的问题是否先修待定。A3 里 DB 重试与 Qwen 超时只有单测、没被真实压测触发，不要说成已在压测中验证。

## 8. 当前缺口

- 实验 A 三档（50 / 500 / 2000）已跑完，基线数字见各记录；A3 的问题已修并在 r3 复测通过（见 A3 r1/r2/r3 记录）；DB 重试与 Qwen 超时尚未被真实压测触发。单测里的百分位仍是假耗时。
- `load_test_chat.py` 已支持 `--user-ids` / `--partnership-ids`；S3 用联采 + 海纳两套 distributor。
- 品牌 RAG 暂无真实品牌资料，golden set 未覆盖；其代码级测试见 §2.1，质量评测待有资料后补充。

- 压测成功样本不足 300 时不输出 P99。`/monitor` 长期账本三档都给，并带样本数。
- 新压测 JSON 会保存每个请求的 `run_id`；历史 JSON 仍需通过会话关联 Run。
- DeepSeek / Qwen / GLM 的官方 RPM、TPM 还没按账户拆进 `KA_ACCOUNT_GATE_MODEL`；当前共用 32，worker 5 远低于官网额度。
- `qwen3.7-flash` 与 `deepseek-flash` 官方价格已接入 `backend/llm/pricing.py`、`load_test_chat.py` 及各轮实验报告（核对日期 2026-11-09）。
- 已记录 S1–S3、A1–A2 的 Qwen/DeepSeek 隐式缓存命中率与实际节省比例；实验 C 再评估是否对长 prompt 开启显式缓存。
- 还没有统一消息模板目录；后续加 `docs/experiments/messages/` 或 `eval/datasets/`，避免每次临时改模板。

