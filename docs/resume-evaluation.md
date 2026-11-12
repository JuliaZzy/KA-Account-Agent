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
  --junitxml=.resume-eval.xml \
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
./dev py scripts/resume_metrics.py --junitxml .resume-eval.xml
```

这里的数字应写成“10 条关键回归用例通过率 100%”，前提是这 10 条在目标提交上全部通过。它不应被写成“线上任务成功率”。这 10 条主要验证功能、数据边界和安全约束，不是压力测试。

## 二、Run 运行时指标

### 合成数据端到端压测

没有真实业务数据时，使用仓库自带的脱敏种子数据。它会生成一个经销商、多个品牌关系、订单和销售事实，时间范围覆盖 2024-01 到 2026-09；`--accounts` 会额外创建可登录的演示用户。这些数据适合压测，不应描述成真实客户数据。

```bash
./dev py scripts/seed_demo.py
./dev py scripts/seed_demo.py --accounts
```

启动 API、worker 和依赖后，用真实 HTTP 链路创建独立会话、提交合成消息，并等待助手回复落库：

种子脚本默认会创建 `ps-liancai-01` 这类 partnership、用户 `user-liancai-chen` 和经销商 `dist-liancai`。如果你的输出使用了其他 ID，就替换成输出中的真实值；不要把尖括号占位符原样输入 shell。

```bash
./dev api
./dev py scripts/load_test_chat.py \
  --base-url http://api:8000 \
  --partnership-id ps-liancai-01 \
  --user-id user-liancai-chen \
  --requests 20 \
  --concurrency 5 \
  --output docs/experiments/exports/load-test-result.json
```

`scripts/load_test_chat.py` 测量的是从 HTTP 提交到 assistant message 写入会话的用户可见耗时。默认消息走空计划路径，适合先测编排器、队列、worker、PostgreSQL、Redis 和 SSE/API 周边的基线；要测查数或报表路径，可以换成合成业务问题，例如：

```bash
./dev py scripts/load_test_chat.py \
  --base-url http://api:8000 \
  --partnership-id ps-liancai-01 \
  --user-id user-liancai-chen \
  --requests 20 \
  --concurrency 5 \
  --message-template '查询最近三个月订单总额，第 {i} 次'
```

也可以用模板文件混合不同路径。每行一个模板，`{i}` 会替换成请求编号：

```bash
cat > docs/experiments/exports/load-test-messages.txt <<'EOF'
你好，第 {i} 次
查询最近三个月订单总额，第 {i} 次
查询本期销售额，第 {i} 次
查找品牌资料中的付款条款，第 {i} 次
EOF

./dev py scripts/load_test_chat.py \
  --base-url http://api:8000 \
  --partnership-id ps-liancai-01 \
  --user-id user-liancai-chen \
  --messages-file docs/experiments/exports/load-test-messages.txt \
  --requests 40 \
  --concurrency 5 \
  --output docs/experiments/exports/load-test-result.json
```

采购和合同路径需要额外的结构化输入或文件附件，不能用一条普通文本消息代表；如果要压这两条路径，应单独写带 `files`/`attachments` 的场景脚本，并把它们单独统计。

这类任务会调用真实配置的模型，可能产生模型调用费用；先用 5 请求做冒烟，再增加到 20、50、100。每一档记录完成率、P50、P95、最大耗时、失败原因，并同时查看 worker 日志和 `/monitor`。

先在 Compose 环境中运行一批合成请求，让 API、worker、PostgreSQL 和 Redis 共同工作。建议每个压测档位至少有 20 条已结束 Run，再看 P95；样本不足时只记录，不把 P95 当稳定结论。

用数据库中已有的 monitor ledger 计算成功率、工具成功率、重复副作用拦截数和完成时间 P95：

```bash
./dev py scripts/resume_metrics.py --org-id dist-liancai
```

如果两类数字都要输出，可以合并调用：

```bash
./dev py scripts/resume_metrics.py \
  --junitxml .resume-eval.xml \
  --load-json docs/experiments/exports/load-test-result.json \
  --org-id dist-liancai
```

简历中建议只保留一行结果，例如：

```text
在 10 条关键回归用例上通过率 100%；使用 [N] 条合成业务请求压测，端到端完成率 [X%]，观测完成时间 P95 [Y 秒]。
```

只有完成率稳定达到 95% 以上时，才考虑把合成压测的 P95 放进简历；否则把结果留在评估记录中，先修复排队、限流、模型超时或 worker 吞吐问题。不要把监控阈值（例如 P95 < 60 秒）写成实测成绩，也不要把合成数据压测写成线上用户结果。

## 三、可写入简历的安全表述

采购规则可以写“价格、SKU、MOQ 和折扣由确定性代码计算”；Text2SQL 可以写“单条只读 SELECT + 作用域视图 + SQL 校验”。

报表分析代码可以写“模型生成的分析代码在受限子进程中执行：AST 禁止导入和私有运行时访问，使用 `python -I`、受限 builtins，并设置 5 秒超时”。如果想使用“沙盒”一词，建议写成“沙盒式受限执行环境”，不要暗示它是独立容器或完整操作系统级隔离。

ReAct、Reflection 和 Memory 应按实际边界描述：ReAct 用于原生工具/MCP 调用；Reflection 用于 Text2SQL 重试、报表审核和分析代码修复；Memory 用于跨轮上下文。不要写成每个 Agent 都无差别执行完整的 ReAct+Reflection 流程。

### 安全边界带来的代价

Text2SQL 当前把分号作为整条语句的拒绝条件。这样可以用很简单、保守的规则挡住多语句和写操作，但也会误拒绝 `WHERE name = 'A;B'` 这类只有字符串值含分号的合法查询，代价是少量查询体验和召回率。简历中应写清楚这是“宁可误杀”的安全取舍；如果以后需要放宽，应先做词法分析，跳过字符串和注释中的分号，再补回归测试，不能只把规则改成字符串替换。

## 四、本次执行记录

你已经在 Docker 环境执行完这 10 条关键回归用例，结果为 **10/10 通过，回归通过率 100%**。这个数字可以写进简历，但必须标成“关键回归用例通过率”，不能写成线上任务成功率或压测完成率。

当前第一轮合成压测记录为：**40 条请求、并发 5，25 条完成，完成率 62.5%；所有请求的观测 P95 180.30 秒，成功请求 P50 7.76 秒、P95 99.73 秒；15 条在 180 秒超时上限内未完成**。这轮只使用了“你好”空计划消息，主要测编排器和运行时基线；结果不适合作为简历亮点，应先定位瓶颈后再复测。

## 五、针对当前简历的修改建议

当前 [docs/cv/resume.html](cv/resume.html) 的 Agent 项目已经压缩到 5 个 bullet，主线比旧版清楚。还需要修正三个地方：

1. 第 120 行的任务编排缺少“按任务类型选择机制”这一层，容易看起来像把所有工作都交给同一套 Agent。建议改成下面的版本，保留 ReAct、Reflection、Memory 关键词，但限定它们的适用范围：

   ```html
   <li><b>任务编排：</b>以 PLAN 将用户请求拆分为采购、合同、报表、品牌检索和数据查询子任务，依据依赖关系分波并行执行；按任务类型选择机制：原生工具/MCP 使用 ReAct 动态调用，Text2SQL、报表审核和分析代码失败时采用 Reflection 重试或改写，采购与合同走固定领域流程，Memory 负责跨轮上下文衔接。</li>
   ```

2. 第 121 行的“Re-Research / Revise / Complete 路由”写得过于全面。当前代码中 Reflection 主要落在 Text2SQL 重试、报表审核和分析代码修复，聊天父 Run 的主路径是 PLAN → 子任务 → ANSWER。建议不要把它写成覆盖所有 Agent 的统一评审路由。

3. 第 122 行可以加入代码执行安全，但应使用准确表述。当前报表分析代码会经过 AST 禁止规则，在 `python -I` 受限子进程中运行，限制 builtins、禁止导入和私有运行时访问，并设置 5 秒超时。建议改成：

   ```html
   <li><b>业务正确性与安全：</b>模型只负责意图理解和内容生成；采购价格、SKU、MOQ、折扣由确定性代码计算，Text2SQL 限制为经销商作用域的单条只读 SELECT；报表分析代码经过 AST 禁止规则后，在使用 <code>python -I</code> 的受限子进程中执行，禁用导入和私有运行时访问并设置 5 秒超时；下单、发信和 MCP 等副作用进入人工确认链路。</li>
   ```

4. 第 117 行已经写了独立负责需求、架构、前后端、数据库和部署，第 100 行个人简介也写了“独立完成 2 个系统从需求到上线”。如果篇幅仍然超长，可以把第 117 行的职责收缩成“独立负责从需求到上线”，把空间留给测试结果。

5. 测试结果拿到后，在第 124 行之后增加一条收尾 bullet，而不是单独再开“量化成果”小节：

   ```html
   <li><b>验证结果：</b>10 条关键回归用例通过率 <b>100%</b>；使用 <b>[N]</b> 条合成业务请求压测，端到端完成率 <b>[Y%]</b>，观测完成时间 P95 <b>[Z 秒]</b>。</li>
   ```

   如果还没有真实 Run，只保留第一句，并把“10 条关键回归用例”明确为测试集结果，不写成线上成功率。
