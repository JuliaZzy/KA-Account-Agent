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

- `tests/test_llm.py::test_vendor_usage_is_actual_and_a_missing_usage_is_estimated`：区分厂商实际 Token 和本地估算；
- `tests/test_llm.py::test_usage_keeps_deepseek_and_qwen_cache_fields`：保留 DeepSeek/Qwen 的缓存命中字段，并规范成统一的缓存 usage 字段；
- `tests/test_llm.py::test_vendor_usage_is_actual_and_a_missing_usage_is_estimated`：同时验证缓存字段会写入 model call 和 usage record；
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
- `tests/test_model_routing.py::test_plan_provider_error_uses_the_configured_fallback`：验证首选模型发生 ProviderError 时切换到备用模型。
- `tests/test_model_routing.py::test_route_events_are_written_to_the_existing_trace`：验证路由选择事件写入已有 Run trace，包含 trace_id 和选中的模型；
- `tests/test_model_routing.py::test_quality_gate_failure_falls_back_and_is_visible_in_trace`：验证 PLAN 质量门槛失败后触发 fallback，并记录质量失败和降级事件。
- `tests/test_model_routing.py::test_quality_gate_failure_falls_back_after_model_completed`：验证真实 `model.completed` 记账后，质量门槛失败仍能降级，不把失败调用算进 `max_model_calls`。
- `tests/test_chat_run.py::test_enqueue_writes_open_run_budget`：验证建 OPEN Run 时写入安全网预算。

这组测试不直接测吞吐或 P95，但属于固定版本回归的一部分。错误的模型路由会改变每个请求的 Token、成本、延迟和质量，因此在性能实验前必须先通过。当前 trace 测试使用轻量账本替身验证事件结构；接入完整数据库 Fixture 后，再增加 HTTP `/api/runs/{run_id}/trace` 的端到端断言。

### 2.5 运行指标和监控接口

这些测试不调真实模型，也不产生第一次实验记录。

- `tests/test_run_api.py::test_the_monitor_lists_one_org_and_reads_the_ledger`：监控只能读取当前组织，并从运行账本生成指标；
- `tests/test_run_api.py` 中 `/metrics` 相关测试：Prometheus 文本和敏感字段过滤；
- `tests/test_performance_metrics.py`：压测 JSON 的百分位算法。20 条成功加 1 条 180 秒超时 → 报 P50 和 P95，P99 留空，成功 P95 不被超时抬高。300 条成功加 1 条超时 → 报 P95 和 P99，P99 不是最慢的那一条。耗时是写进 JSON 的假数字；
- `tests/test_monitor_metrics.py`：空样本百分位是 `null`；P50 ≤ P95 ≤ P99；完成时间只含成功 run；失败和仍在排队的 run 不进完成时间样本；直方图有 180s 和 300s 桶，模型桶更短；`/metrics` 文本没有 `run_id` / `session_id` / `user_id`；
- `backend/services/monitor.py`：账本上的 P50/P95/P99、阶段耗时、PLAN 错误率和 SLO 告警。

运行监控给出 P50、P95 和 P99，空样本是 `null`。压测里成功样本不足 300 只报 P50 和 P95；300 条及以上才报三档。失败和超时不进入成功样本的百分位。阶段耗时从已有时间戳读：排队 = `started_at - created_at`，模型 / 工具来自各自的 `latency_ms`，完成时间 = `ended_at - started_at`。没有单独的 `tests/test_runtime_timing.py`。

超时、并发、租约和 SLO 的默认值在 [`../architecture/architecture.md`](../architecture/architecture.md) 的「当前运行标准」。

## 3. 实验怎么跑

单测锁住算法和契约。第一次实验记录来自下面的压测，调真实模型。顺序固定：

1. 对照官网把各模型 RPM/TPM 填进架构文档「当前运行标准」，再决定 worker 和 `--concurrency`，不要先写成 5 或 10；
2. `./dev lint` 和 `./dev test`；
3. 用固定消息模板跑第一轮压测，保存 JSON、提交号、worker 并发和模型配置；
4. 把数字填进第 7 节模板，标成「第 1 次」；
5. 按第 6 节的调查顺序看未完成和过慢的请求；
6. 改一处明确瓶颈后再跑第二轮，模板、并发、timeout 不变，标成「第 2 次」。

不要把单测里的 20 / 300 当成已经测过的系统耗时。

「先 50 条冒烟，再 100 条基线」是更早一版的说法，和现在的 20 / 300 是同一件事的两个阶段：

- **冒烟**：条数少，只确认入队、worker、模型、回写这条链能跑通，少花 token。原来写 50，现在用 20，因为 20 条成功已经够报 P50 和 P95。
- **基线**：固定样本量，记下改代码之前的数字，给第 2 次对比。原来写 100，100 条的 P95 比较稳，P99 仍是倒数第 2 条，所以要报 P99 时把成功样本凑到 300。

问候路径 PLAN/ANSWER 都走官方 Qwen（`qwen3.7-flash`）。冒烟并发默认 5，不要超过 worker 和 `GATE_USER`。

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

- [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/) 和 [DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing/)；
- [Qwen Context Cache](https://help.aliyun.com/en/model-studio/context-cache)。

隐式缓存通常不需要应用侧配置；显式缓存需要稳定且足够长的前缀，并有创建成本和有效期。
在没有命中数据之前，不把显式缓存写进通用调用层，也不把固定 system prompt 默认标记为可缓存。

## 4. 固定执行顺序

每次版本更新按以下顺序执行：

1. `./dev lint`；
2. `./dev test`，确认代码和领域回归；
3. 生成 JUnit XML，汇总关键回归通过率；
4. 执行模型路由、降级和指标计算回归测试；
5. 使用脱敏 Fixture 执行离线性能指标测试；
6. 启动 Compose API、worker 和依赖，执行小规模端到端冒烟；
7. 按固定场景、请求数和并发执行压测；
8. 读取 Run ledger 和 `/monitor`，核对 Token、成本、队列、模型和工具指标；
9. 把原始 JSON、提交号、配置和结论写入实验记录。

命令入口：

```bash
./dev test --junitxml=.resume-eval.xml
./dev py scripts/resume_metrics.py --junitxml .resume-eval.xml
./dev py scripts/load_test_chat.py \
  --base-url http://api:8000 \
  --partnership-id ps-liancai-01 \
  --user-id user-liancai-chen \
  --requests 10 \
  --concurrency 5 \
  --output docs/experiments/exports/load-test-smoke.json
```

10 条冒烟确认链通；20 条成功样本再看一条路径的 P50 和 P95。要报 P99，把同一路径的成功样本凑到 300。不要按 agent 把 100 条拆开各算一个 P99。优化前后使用相同请求模板、相同并发和相同 completion timeout。不同业务路径分开跑。并发不超过架构文档里 `min(worker, GATE_ORG, GATE_USER, GATE_MODEL, 官网 RPM)`。

## 5. 第 8 条实验计划

### 实验 A：运行链路基线

目的：测量 API、outbox、Redis、Celery、Postgres、Worker 和 SSE/API 周边的基础耗时。

- 输入：空计划或简单问候模板；
- 请求数：先 20 条看 P50 和 P95；要 P99 时把成功样本凑到 300；
- 并发：先用现在的 worker 8；对照官网 RPM 之后再加，不要先试 20；
- 指标：完成率、超时数、成功 P50/P95（样本够再加 P99）、队列等待、模型调用、Token 和成本。

### 实验 B：领域路径基线

目的：分别测量 Text2SQL、品牌检索、报表和采购路径，定位模型、检索、数据库或工具耗时。

- 每个场景先 20 条看 P50 和 P95；
- 要报该场景 P99 时，成功样本凑到 300，不要 50～200 就写 P99；
- 采购确认、发信和 MCP 副作用不直接压测真实外部系统，使用 Fake 或 dry-run 适配器；
- 同时检查质量回归和副作用重复拦截。

### 实验 C：上下文和 Token 优化

比较历史消息裁剪、长期记忆摘要、工具 Schema 精简和模型调用预算变化。

- 固定同一批质量评测样本；
- 记录平均、P50 和 P95 的 Prompt Token、Completion Token、单 Run 成本；
- 记录可用样本的缓存命中 token、未命中 token、缓存创建 token 和命中率，并按 provider/model 分开；
- 记录命中率与模型调用延迟的变化，不能只看 `prompt_tokens` 是否减少；
- 质量下降超过预先设定阈值时，优化不通过。

### 实验 D：并行和队列优化

比较单波串行、依赖分波和多个 Worker 并发下的完成时间、队列等待、吞吐和失败率。

- 使用 `tests/test_fanout.py` 的多任务场景；
- 使用 3、5、10 个 Worker 并发配置做独立实验；
- 关注模型 provider 并发上限，不能只增加 Worker 数量。

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
7. 修复一个明确瓶颈后，用相同模板和相同并发复测。先看 P50/P95；要写 P99 再用 300 条成功样本。

可能原因包括模型 provider 限流、模型槽位不足、Worker 并发与 provider 限额不匹配、队列等待、Run 重试、会话锁、SSE 轮询观察误差或 completion timeout 偏短。必须通过 trace 和日志确认，不能凭完成率猜测。

## 7. 实验结果记录模板

正式实验写在 [`docs/experiments/`](../experiments/)，一份实验一个 Markdown。下面是字段清单；填完后不要只留在聊天里。

```text
实验名称：
第几次：第 1 次 / 第 2 次
代码提交：
数据集/消息模板：
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

已记录：[`2026-11-09-qwen-smoke-concurrency-5.md`](../experiments/2026-11-09-qwen-smoke-concurrency-5.md)（官方 Qwen 问候、并发 5、8/10）。

## 8. 当前缺口

- 第 1 次正式实验还没跑。20 / 300 只存在于 `tests/test_performance_metrics.py` 的假耗时。
- 压测脚本在成功样本不足 300 时不输出 P99。`/monitor` 长期账本同时给 P50、P95、P99，并带样本数。
- 当前压测结果没有把每个请求的 `run_id` 写入结果 JSON，调查未完成请求时要对会话再查一次。
- DeepSeek / Qwen / GLM 的官方 RPM、TPM 还没按账户拆进 `KA_ACCOUNT_GATE_MODEL`；当前共用 32，worker 8 远低于官网额度。
- 当前成本记录有 Token 和估算成本字段，真实金额还要确认每个 provider/model 的价格配置和版本。
- 当前已记录 DeepSeek/Qwen 的缓存 usage 字段，但尚未有真实供应商压测样本，因此还不能判断哪些 prompt 值得显式缓存。
- 当前没有统一的固定消息模板目录；后续应新增 `tests/performance/messages/` 或 `eval/datasets/`，避免每次临时输入不同。

