# 简历结果指标评估方案

简历里的数字要分成两类：测试集结果和实际 Run 运行结果。前者说明能力闭环是否通过，后者说明运行时在真实数据库和队列中的表现。两类数字不能混写。

## 一、关键回归用例

先固定一组跨业务场景，每次代码变更后重复执行。当前仓库已有测试覆盖这些场景，可以用下面 10 条作为第一版评估集：

1. `tests/test_app.py::test_full_flow`：HTTP 建立会话、后台 Run 完成并写回回复。
2. `tests/test_submit.py::test_confirm_creates_one_order_and_burns_the_token`：采购草稿确认后只建一张订单，确认 token 在建单前消费。
3. `tests/test_contract_read.py::test_read_and_file_an_agreement`：合同读取、结构化抽取和确认入库。
4. `tests/test_report_agent.py::test_review_passes_at_85_and_revises_below_it_for_three_rounds`：报表审核低于 85 分时改写并重新审核。
5. `tests/test_report_agent.py::test_insight_code_reads_the_rows_and_rejects_an_import`：分析代码读取数据，并拒绝导入。
6. `tests/test_brand_rag.py::test_brand_question_is_retrieved_then_answered_by_the_text_model`：品牌资料检索和带来源回答。
7. `tests/test_text2sql.py::test_a_question_only_sees_this_partnership`：查数只返回当前合作关系的数据。
8. `tests/test_text2sql.py::test_a_write_is_refused_and_the_order_stays`：写操作被拒绝，业务数据不改变。
9. `tests/test_fanout.py::test_same_wave_is_two_child_runs`：无依赖任务并行入队，子 Run 完成后父 Run 恢复并汇总。
10. `tests/test_run_api.py::test_a_worker_continues_from_a_tool_result_without_calling_it`：从 checkpoint 继续执行，已完成工具不重复调用。

执行方式：

```bash
./dev test \
  --junitxml=/tmp/resume-eval.xml \
  tests/test_app.py::test_full_flow \
  tests/test_submit.py::test_confirm_creates_one_order_and_burns_the_token \
  tests/test_contract_read.py::test_read_and_file_an_agreement \
  tests/test_report_agent.py::test_review_passes_at_85_and_revises_below_it_for_three_rounds \
  tests/test_report_agent.py::test_insight_code_reads_the_rows_and_rejects_an_import \
  tests/test_brand_rag.py::test_brand_question_is_retrieved_then_answered_by_the_text_model \
  tests/test_text2sql.py::test_a_question_only_sees_this_partnership \
  tests/test_text2sql.py::test_a_write_is_refused_and_the_order_stays \
  tests/test_fanout.py::test_same_wave_is_two_child_runs \
  tests/test_run_api.py::test_a_worker_continues_from_a_tool_result_without_calling_it
```

然后生成简历用的通过率：

```bash
./dev py scripts/resume_metrics.py --junitxml /tmp/resume-eval.xml
```

这里的数字应写成“10 条关键回归用例通过率 100%”，前提是这 10 条在目标提交上全部通过。它不应被写成“线上任务成功率”。

## 二、Run 运行时指标

先在 Compose 环境中运行一批真实或脱敏请求，让 API、worker、PostgreSQL 和 Redis 共同工作。建议至少积累 20 条已结束 Run，再看 P95；样本不足时只记录，不把 P95 当稳定结论。

用数据库中已有的 monitor ledger 计算成功率、工具成功率、重复副作用拦截数和完成时间 P95：

```bash
./dev py scripts/resume_metrics.py --org-id <distributor-id>
```

如果两类数字都要输出，可以合并调用：

```bash
./dev py scripts/resume_metrics.py \
  --junitxml /tmp/resume-eval.xml \
  --org-id <distributor-id>
```

简历中建议只保留一行结果，例如：

```text
在 10 条关键回归用例上通过率 100%；基于 [N] 条已完成 Run，运行成功率 [X%]，完成时间 P95 [Y 秒]。
```

没有真实 Run 数据时，删掉第二句；不要把监控阈值（例如 P95 < 60 秒）写成实测成绩。

## 三、可写入简历的安全表述

采购规则可以写“价格、SKU、MOQ 和折扣由确定性代码计算”；Text2SQL 可以写“单条只读 SELECT + 作用域视图 + SQL 校验”。

报表分析代码可以写“模型生成的分析代码在受限子进程中执行：AST 禁止导入和私有运行时访问，使用 `python -I`、受限 builtins，并设置 5 秒超时”。如果想使用“沙盒”一词，建议写成“沙盒式受限执行环境”，不要暗示它是独立容器或完整操作系统级隔离。

ReAct、Reflection 和 Memory 应按实际边界描述：ReAct 用于原生工具/MCP 调用；Reflection 用于 Text2SQL 重试、报表审核和分析代码修复；Memory 用于跨轮上下文。不要写成每个 Agent 都无差别执行完整的 ReAct+Reflection 流程。
