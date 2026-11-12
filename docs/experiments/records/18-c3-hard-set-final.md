# 实验 C3：最终 seed_v2 开发集上的 workflow 与 ReAct 对照

- **目的**：在修正用户归属口径、聚合结果检查、精确截断计数、产品规格匹配和并列规则之后，重新比较旧 workflow 与 ReAct。
- **数据**：最终版 `seed_v2`，金标为 `golden_hard_v2.jsonl`。旧的 seed_v1 文件和实验记录保留不动。
- **题目**：奇数开发集 `t2s-13/15/17/19/21/23/25/27/29/31` 加 `rpt-09`，每题 3 次，并发 1，共 33 次/方案。
- **限制**：正式 LIMIT 100/200；不是之前用于触发截断的 50/80。
- **实现**：`KA_TEXT2SQL_IMPL=react|legacy`。规划、回答阶段和报告路径保持相同；只切换 data query 的内部实现。

## 本轮改动

1. `users` 和 `sessions` 不再进入 Text2SQL schema。ORM 虽然有工作台用户和会话字段，但 `purchase_orders` 没有用户归属字段，采购单按 partnership/distributor 归属。
2. `t2s-25` 改成经销商维度对照题：“这个经销商一共下了多少张采购单？”，seed_v2 金标为 67。
3. 系统给查询触顶结果执行额外的 COUNT 包装查询，`row_count` 返回真实总行数，同时保留 `truncated=true`。本轮 `t2s-27` 的真实总数为 201。
4. `deliver=answer` 不再强制 planner 选出的下游字段。纯对话问题不应因为 `order_id`、`amount` 这类多余 required columns 拒绝已经正确的聚合结果。
5. 聚合、全部/所有、产品名称与规格、并列最大值的规则同步到 ReAct 和旧 workflow。

## 结果

| | text2sql | report | 首条 SQL 成功 | P50 | P95 | 模型调用/次 | ¥/次 |
|---|---:|---:|---:|---:|---:|---:|---:|
| ReAct | **30/30** | 3/3 | 29/33 | 9.27s | 17.43s | 7.82 | 0.00859 |
| 旧 workflow | **24/30** | 3/3 | 30/33 | 9.75s | 15.80s | 8.52 | 0.00538 |

成本按本轮账本记录，运行时段相同。ReAct 每次成本约高 60%，但平均模型调用次数更少；P50 略快，P95 本轮旧 workflow 略好。延迟差异不稳定，不能据此下结论。

## 失败差异

### `t2s-23`：产品名称和规格

- ReAct 3/3：查询 `product_name_en = 'Energy Drink'`，再用 `unit_size/net_content` 匹配 250 ml，得到 440 箱。
- 旧 workflow 3/3：把用户写的“Energy Drink 250ml”当成完整产品名，查询 `product_name_en = 'Energy Drink 250ml'`，得到空结果后直接回答“没有数据”。

这体现了 ReAct 在看到空结果后修正查询语义的优势。

### `t2s-27`：结果触顶

- ReAct 3/3：不把系统返回的 100 行当成全部，结果视图显示真实 `row_count=201` 和前 50 行，回答说明“共 201 行、展示前 50 行”。
- 旧 workflow 3/3：模型自行加入 `LIMIT 200`，或把聚合后的 200 行当作总数，回答“共 200 行/100 行”，没有拿到真实总数。

这体现了代码提供精确总数后，ReAct 更能遵守观察结果；旧 workflow 的 SQL 重写仍可能自行加入限制。

### `t2s-13`：平均值聚合

两边最终都是 3/3。ReAct 首条 SQL 成功 2/3，旧 workflow 1/3；失败的首条查询主要是返回月度明细或类型错误，之后通过重写得到平均值。

### `t2s-19`：类型错误修复

ReAct 首条 SQL 0/3，但最终 3/3；旧 workflow 首条 SQL 2/3，最终也是 3/3。本轮没有显示出最终准确率差异，只能说明 ReAct 能在观察错误后完成修复。

### `t2s-25`：经销商归属

两边 3/3。问题已经改成采购单支持的粒度，均回答经销商共有 67 张采购单。用户归属不再作为评测目标。

### `t2s-31`：并列最多

两边 3/3。两边都能报告 2025 年 4、8、10、12 月并列最多，每月 4 张。

## 结论

在这批真正包含 SQL 错误、空结果和截断结果的开发题上，ReAct 的最终通过率高于旧 workflow：30/30 对 24/30。优势主要来自两类行为：看到空结果后重查，以及根据结果观察修正查询；精确截断总数的代码契约也让 ReAct 的结果表达正确。

代价是单次成本约高 60%。本轮 P50 只快约 0.5 秒，P95 没有优势，所以 ReAct 的价值主要是正确性和自修复，不是延迟。

这仍然只是开发集结果。下一步应冻结代码和提示词，只跑偶数留出集一次；留出集跑完后不能再根据结果修改实现。

## 文件

- ReAct：`docs/experiments/exports/golden-hard-dev6-react.jsonl`、`golden-hard-dev6-react-score.log`
- 旧 workflow：`docs/experiments/exports/golden-hard-dev6-legacy.jsonl`、`golden-hard-dev6-legacy-score.log`
- 金标：`docs/experiments/golden_set/golden_hard_v2.jsonl`
