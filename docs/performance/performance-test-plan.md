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
- `tests/test_session_concurrency.py::test_one_session_finishes_a_turn_before_the_next_starts`：同一会话保持写入顺序。

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

当前只观察 DeepSeek 和 Qwen 的供应商缓存结果。SJTU、GLM 不在本轮缓存优化范围内。

### 2.4 运行指标和监控接口

- `tests/test_run_api.py::test_the_monitor_lists_one_org_and_reads_the_ledger`：监控只能读取当前组织，并从运行账本生成指标；
- `tests/test_run_api.py` 中 `/metrics` 相关测试：Prometheus 文本和敏感字段过滤；
- `backend/services/monitor.py`：实际生成完成时间、队列等待、模型延迟、工具错误、成本和 SLO 告警。

目前已有 P50/P95 的压测计算和 P95 的运行监控，但生产监控和端到端压测尚未统一提供 P99。P99 是本框架的待补项，不能在补代码前写成已有指标。

## 3. 需要新增的固定测试

### 3.1 指标计算测试

已新增：`tests/test_performance_metrics.py`

用途：验证百分位计算、压测 JSON 汇总和指标字段不会因为重构产生静默变化。它只测试纯函数和 JSON，不请求真实模型，也不需要启动完整压测。

当前覆盖和后续扩展：

- `scripts/load_test_chat.py::_percentile` 在空样本、单样本、偶数样本和尾部样本下的结果；
- `scripts/resume_metrics.py::load_test_metrics` 对完成率、超时、观测 P50/P95 和完成请求 P50/P95 的解析；
- 新增 P99 后，验证观察样本和成功样本分别计算 P99；
- 结果缺失、字段为空或混合失败样本时，不产生虚假的成功延迟。

这组测试目前只验证已有的 P50/P95 汇总逻辑；P99 字段加入后再补对应断言。

### 3.2 监控指标契约测试

新增：`tests/test_monitor_metrics.py`

用途：固定 `/metrics` 和 `/monitor` 的字段、类型、组织隔离和直方图结构，防止只改了页面却丢掉后端指标。

计划覆盖：

- `agent_run_duration_seconds`、`agent_queue_lag_seconds`、`agent_model_call_latency_seconds` 的 histogram 结构；
- `agent_estimated_cost_cents_total`、Token、工具错误和重复副作用指标；
- `/monitor` 的 P50/P95/P99 字段在无样本时为 `null`，不是 0；
- 组织 A 不能读取组织 B 的运行指标。

### 3.3 分阶段耗时测试

新增：`tests/test_runtime_timing.py`

用途：验证 Run trace 能区分入队、排队、模型、工具和完成阶段，为后续优化提供可解释数据。

当前代码还没有完整的阶段耗时字段。先补测试契约，再实现字段和数据库读取：

- queue lag = `started_at - created_at`；
- model latency 来自 `model_calls.latency_ms`；
- tool latency 来自 `tool_calls.latency_ms`；
- end-to-end duration = `ended_at - created_at`；
- P99 与 P95 使用同一批原始样本计算。

### 3.4 前置缓存观测测试

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
4. 使用脱敏 Fixture 执行离线性能指标测试；
5. 启动 Compose API、worker 和依赖，执行小规模端到端冒烟；
6. 按固定场景、请求数和并发执行压测；
7. 读取 Run ledger 和 `/monitor`，核对 Token、成本、队列、模型和工具指标；
8. 把原始 JSON、提交号、配置和结论写入实验记录。

命令入口：

```bash
./dev test --junitxml=.resume-eval.xml
./dev py scripts/resume_metrics.py --junitxml .resume-eval.xml
./dev py scripts/load_test_chat.py \
  --base-url http://api:8000 \
  --partnership-id ps-liancai-01 \
  --user-id user-liancai-chen \
  --requests 50 \
  --concurrency 3 \
  --output .load-test-smoke.json
```

50 条请求用于冒烟；稳定比较至少使用 100 条，优化前后使用相同请求模板、相同并发和相同 completion timeout。不同业务路径分开跑，不把空计划、Text2SQL、RAG 和报表混合计算一个 P95。

## 5. 第 8 条实验计划

### 实验 A：运行链路基线

目的：测量 API、outbox、Redis、Celery、Postgres、Worker 和 SSE/API 周边的基础耗时。

- 输入：空计划或简单问候模板；
- 请求数：先 50 条冒烟，再 100 条基线；
- 并发：3、5 两档；
- 指标：完成率、超时数、P50/P95/P99、队列等待、模型调用、Token 和成本。

### 实验 B：领域路径基线

目的：分别测量 Text2SQL、品牌检索、报表和采购路径，定位模型、检索、数据库或工具耗时。

- 每个场景至少 50 条初始请求；
- 有稳定结果后扩展到每场景 100～200 条；
- 采购确认、发信和 MCP 副作用不直接压测真实外部系统，使用 Fake 或 dry-run 适配器；
- 同时检查质量回归和副作用重复拦截。

### 实验 C：上下文和 Token 优化

比较历史消息裁剪、长期记忆摘要、工具 Schema 精简和模型调用预算变化。

- 固定同一批质量评测样本；
- 记录平均和 P95 Prompt Token、Completion Token、单 Run 成本；
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

这轮记录来自 `scripts/load_test_chat.py`：40 条请求、并发 5，25 条完成，完成率 62.5%，15 条在 180 秒 completion timeout 内没有完成。观测 P95 为 180.30 秒，成功请求 P50 为 7.76 秒、P95 为 99.73 秒。

这不能直接归因于某一个组件，也不能当作性能成果。现有数据只能说明：部分请求很快完成，但尾部请求持续排队、重试或未能在超时前写回 assistant message。

调查顺序固定为：

1. 保存每个请求的 `session_id`、`run_id`、提交时间、完成时间和错误字段；
2. 对未完成请求查询 `/api/runs/{id}/trace` 和数据库 `agent_runs`；
3. 区分 `queued`、`running`、`waiting_for_tasks`、`waiting_for_user`、`failed` 和终态；
4. 查看 `queue_lag`、`model_calls.latency_ms`、`tool_calls.latency_ms`、429、超时和 `retry_after_seconds`；
5. 查看 Worker 日志和 `pending_jobs`、`active_runs`、`stuck_runs`；
6. 检查是否在同一会话中提交了多条消息、是否受到会话锁或 provider 并发限制；
7. 修复一个明确瓶颈后，用相同 40 条输入复测，再用 100 条固定样本确认结果。

可能原因包括模型 provider 限流、模型槽位不足、Worker 并发与 provider 限额不匹配、队列等待、Run 重试、会话锁、SSE 轮询观察误差或 completion timeout 偏短。必须通过 trace 和日志确认，不能凭完成率猜测。

## 7. 实验结果记录模板

```text
实验名称：
代码提交：
数据集/消息模板：
请求数：
并发数：
Worker 数与配置：
模型/provider：
completion timeout：
完成率：
超时与失败原因：
P50：
P95：
P99：
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

## 8. 当前缺口

- `scripts/load_test_chat.py` 当前输出 P50、P95 和最大耗时，尚未输出 P99。
- `backend/services/monitor.py` 当前主要展示 P95，尚未按阶段提供 P99。
- 当前压测结果没有把每个请求的 `run_id` 写入结果 JSON，调查 62.5% 完成率时需要补齐关联信息。
- 当前成本记录有 Token 和估算成本字段，但要做真实金额比较，还需要确认每个 provider/model 的价格配置和版本。
- 当前已记录 DeepSeek/Qwen 的缓存 usage 字段，但尚未有真实供应商压测样本，因此还不能判断哪些 prompt 值得显式缓存。
- 当前没有统一的固定消息模板目录；后续应新增 `tests/performance/messages/` 或 `eval/datasets/`，避免每次临时输入不同。

