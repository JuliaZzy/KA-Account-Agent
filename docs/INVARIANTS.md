# 不变量

不变量：无论怎么改代码都必须保持为真的规则。

让 AI 改 `backend/` 之前，先读本文件，并在回复里说明改动涉及哪几条。

## INC-1 红牛月报父 run 空转

2026-11-23 红牛月报，子 run `run_57fa43cc17e34829ba6fbf3b97db02c8`。

同一次月报里，`data.react` 三路并发都成功。`report.optional` 的 `code_wizards` 在已经跑着的事件循环上调用 `run_until_complete`，报了 `This event loop is already running`，被记成 `PROVIDER_INTERNAL`。`TaskGroup` 写明一个失败会取消另一个，图表的 6 次调用仍继续成功。子 run 失败之后没有交给父 run，父 run 空转了约 95 次。

后面的原因写「见 INC-1」。

### INC-1 结案

- I-1 并发：`5e9020f`，`3d85701`
- I-2 lease_owner 随机后缀：`20df844`
- I-3 终态出口、对账、上限：`20df844`
- I-4 classify：`96c39a7`（分类函数和映射表），`20df844`（worker、四处 except、换模型和熔断），`fc918e2`（`JsonRepairExhausted` 和额度用尽不再各写一套 except）
- I-5 可选步骤降级：`03f539e`。结案行：`6569c50`。可选步骤失败直接降级、不在报告内重试：`fc918e2`
- I-6、I-7：`5e9020f`（检查脚本），`3d85701`（删掉遗留 text2sql）
- 脚本接入 CI：`b4304fb`
- 结案记录：`aadd2cb`，`6569c50`
- 测试跟上异步的合同读取和审稿 schema：`a8d626d`
- 博查 HTTP 错误交给 classify：`d535f60`。403 是额度或套餐不够，码是 `PROVIDER_AUTHENTICATION`

到 `fc918e2` 为止，INC-1 已结案。干净重放和注入重放都已跑过：月报成功，或带着 `degraded` 成功，父 run 不再空转。

## I-1 并发只用 asyncio

- 规则：异步代码里只 `await` 异步函数。`asyncio.run` 只允许出现在进程入口，每个入口文件一处。允许一个专用线程里的事件循环，只在 `backend/mcp/runtime.py`：只通过 `run_coroutine_threadsafe` 提交协程，不在调用方线程上 `run_until_complete`，单例创建带锁。MCP 会话绑在创建它的循环上，worker 每次执行都是新的 `asyncio.run`，会话没法跨 run 存活，所以需要这一个长生命周期的循环。
- 原因：见 INC-1。调模型有过两套实现。`data.react` 走 `_ask_ds_async` → `deepseek_json_async` → `call_model_async`，全程 `await`，三路并发都成功。`report.optional` 走到同步的 `_call_deepseek` → `_await` → `run_until_complete`，撞上已经在跑的循环。
- 检查：由 `scripts/check_invariants.sh` 执行。白名单分成两类。`asyncio.run(` 每个入口文件一处。`new_event_loop` 只允许专用线程那一处。`run_until_complete`、`_await`、`_background_loop` 整词匹配必须为空。注释行不算命中。白名单是允许留下的位置，不是把今天的命中抄进去。
- 测试：一个 `TaskGroup` 里同时发起两次模型调用，重复跑几十遍，两次都返回，没有 `This event loop is already running`。两个线程同时要 MCP 的循环，拿到的是同一个 loop。

`asyncio.run` 的入口：`backend/services/run_worker.py`（worker 执行协程）、`backend/contract/read.py`（命令行 `main`）、`backend/llm/client.py`（`if __name__` 脚本）。`new_event_loop` 只允许 `backend/mcp/runtime.py`。同一文件里的第二处算违反。

## I-2 互斥只靠数据库状态转换

- 规则：跨 worker 的互斥只靠数据库状态转换。认领一条 run 使用 `status == queued` 加 `with_for_update`，认领后改为 `running`，并把 `lease_owner` 写成这次认领生成的值。这个值每次认领都不同，带随机后缀，不是 `celery:{pid}` 这种进程名。同一个进程再次认领同一条 run 时，旧的 `lease_owner` 对不上。`finalize_run` 更新终态时，`WHERE` 带上这次的 `lease_owner`。更新影响 0 行，这次写入不发生。不另加版本列。
- 原因：见 INC-1。这次事故不是两个 worker 同时执行了同一个 run。父 run 的唤醒和 worker 的认领已经用状态转换：`requeue_waiting_tasks` 锁 `waiting_for_tasks`，`claim_chat_turn` 锁 `queued`，第二条消息看到的行已经不是 `queued`，直接返回。留下的口子是租约中途过期：sweeper 把 `running` 改回 `queued`，另一个进程可以再认领并再跑一遍。旧 worker 若在执行结束后仍然写终态，两个 worker 会先后各写一次。
- 检查：终态更新的语句里带 `lease_owner` 条件。新增的互斥不靠进程内的锁或全局可变对象挡住另一个 worker。
- 测试：同一 `run_id` 的两条执行消息同时认领，只有一条进入执行。同一个进程（同一个 pid）在租约过期后再次认领，旧的 `lease_owner` 写不进终态，行保持第二次认领的结果。

## I-3 终态和父 run

- 规则：worker 收口走 `finalize_run`：一个事务里 `_write_terminal` 写终态，提交后 `after_terminal`。清扫和次数用尽在调用方已有的事务里调 `_write_terminal`，提交后 `after_terminal_row`。`RunStatus` 的终态是 `succeeded`、`failed`、`cancelled`。死信是 `dead_letter_jobs` 里的一行，run 仍是 `failed`。排标题、通知父 run、写失败对话在事务外。`notify_parent` 失败只记 warning。下一轮 `_repair_stale_owners` 按 checkpoint 把缺的子结果补上。唤醒以数据库为准；Redis 计数归零只是提前入队。
  经 `_write_terminal`：worker 成功、worker 取消、`_settle` 的永久失败、worker 超时、时长清扫（`running`，以及父 run 停在 `waiting_for_tasks` 且已超过自己的 `max_duration_seconds`）、租约清扫的三个终态（内联导入失败、已请求取消、执行次数用尽）、`schedule_retry` 执行次数用尽、`mark_outbox_retry` 发布次数用尽（先 `queued → running`，再失败）。`COST_BUDGET_EXCEEDED` 的 worker 超时和 sweeper 时长清扫都写死信。
  经 `_write_terminal`（`_fail_locked_run` 只是转给它），提交后 `after_terminal_row`：队列清扫里卡住的 run、worker 丢失重投用尽、容量等待超时。API 取消 `queued` / `waiting_for_user` / `waiting_for_tasks`，以及合同内联导入成功，直接 `_transition`。内联导入的租约过期走 `_write_terminal`，失败且不写死信。`complete_run`、`fail_run`、`cancel_run` 已删除，调用方走 `finalize_run` 或 `finalize_run_row`。
- 对账：`_repair_stale_owners`。子 run 已终态，而父 checkpoint 的 `orchestrator.results` 没有这个 `task_id` 时，调用 `settle_terminal_child`。有活子 run（`queued`、`running`、`waiting_for_tasks`）时不补结果、不计数、不唤醒。
- 上限：连续空转的次数在父 run 最新 checkpoint 的 `state_json.orchestrator.stale_wakes`，整数，不是 `agent_runs` 的列。父 run 另受自己的 `max_duration_seconds` 约束；这一列为空则时长清扫不关它。子 run 都已结束且这一轮没有新结果时 `note_stale_wake` 加一，有新结果则写成 0。补上原先缺失的子结果也算进展，同一次清扫把 `stale_wakes` 写成 0，不是漏加。达到 `STALE_WAKE_LIMIT`，`_force_unfinished` 写入 `WAVE_STALE` 并以 `stale_limit` 重新入队。未到 `STALE_WAKE_LIMIT` 时，同一次清扫里的 `sweep_waiting_for_tasks` 以 `waiting_tasks_stale` 入队。父 run 再执行时 `dispatch_wave` 返回空，走 `_answer`。
- 原因：见 INC-1。子 run 遇到 `ProviderError` 后直接抛出，worker 只调用了 `fail_run`，没有 `finish_child`。父 run 停在 `waiting_for_tasks`。sweeper 看到子 run 已经结束，只把父 run 重新入队，checkpoint 里仍然没有结果。父 run 再派发时命中已经失败的幂等键，又停回去。
- 检查：由 `scripts/check_invariants.sh` 执行。`fail_run` 整词匹配必须为空。`_write_terminal` 只定义在 `backend/db/database/run.py`。`_transition(` 到 `SUCCEEDED` / `FAILED` / `CANCELLED`，以及 `transition_run(` 带这三个状态，只允许出现在白名单 `[terminal_transition]`。`_fail_locked_run` 内部调 `_write_terminal`，不再自己 `_transition` 到 `failed`。
- 测试：`test_finalize_all_paths`。在每个失败点注入故障（`ProviderError`、容量超限、取消、进程被杀、租约过期），断言父 run 在有限次数内进入 ANSWER。对账和上限分开断言：补上终态子结果时 checkpoint 有结果并且 `stale_wakes` 归零，父 run 被入队；子 run 仍在运行时不补、不计数、不唤醒；连续 3 次没有新结果时写入 `WAVE_STALE` 并入队，再次执行走 ANSWER，不再停回 `waiting_for_tasks`。同一状态重新派发时 `stale_wakes` 保持为 1、2、3。

## I-4 错误分类只有一个函数

- 规则：所有异常经过 `backend/resilience/errors.py` 里的 `classify(exc) -> ErrorKind`。kind 由抛出它的边界声明（`ProviderError.kind`、`ModelCapacityExceeded`、`ModelOutputError` / `JsonRepairExhausted`、`ModelBudgetExceeded`、`PermanentRunError`、`ClassifiedError`）。`classify` 只读这个声明，再查 `backend/resilience/policy.py`，不根据异常类型猜测。没有声明的异常，以及 `ProviderError` 上不在策略表里的 kind（`internal`、`event_loop`），都是 `unknown`，码是 `UNKNOWN`，不重试、不换模型、不进熔断，`error_json` 写 `exc_type` 和 `traceback`。不记成 `PROVIDER_INTERNAL`。供应商超时仍是 `ProviderError.kind == timeout`，走基础设施重试。`invalid_json` 走业务重试，扣次数，上限 3。换模型看策略表的 `fallback_group` 是否在路由的 `fallback_on` 里（组名仍是 `provider`、`capacity`、`invalid_json`）。熔断只计入策略表里 `breaker` 为真的 kind。
- 原因：见 INC-1。这次的 `RuntimeError` 被收成 `ProviderError` / `PROVIDER_INTERNAL`。供应商没有失败，本地循环被重入了。按供应商错误处理之后，这条调用不重试、不换模型，整条月报被判死。后来把未声明的异常再重试一次，只会再烧一次调用，也把本地 bug 掩盖掉。
- 检查：由 `scripts/check_invariants.sh` 执行。`except ProviderError`、`except ModelCapacityExceeded`，以及只包含这两类的元组，只出现在 `backend/resilience/errors.py`。`except` 行里出现 `JsonRepairExhausted` 或 `ModelBudgetExceeded` 也只允许在这个文件。`"provider_error"` 和 `"model_capacity"` 也只允许出现在这个文件。脚本列出的调用点改为 `except Exception`，再 `kind = classify(exc)`，要交给 worker 的用 `hands_off_to_worker(kind)`。
- 测试：`This event loop is already running` 分类为 `unknown`，码是 `UNKNOWN`，不是 `PROVIDER_INTERNAL`，不进入供应商熔断，不触发换模型，worker 不重试。

## I-5 可选步骤失败要降级

- 规则：一步算不算可选，只看下面这张表。表里没有的步骤默认不可选，失败则这条 run 失败，父 run 进入 ANSWER。新增步骤先登记，再写代码。用途名 `report.optional` 不决定是不是判死整条月报。

| 步骤 | schema | 可选 | 失败之后 | 报告里怎么体现 |
|---|---|---|---|---|
| 大纲 | `report_plan` | 否 | 整份月报失败，父 run 进入 ANSWER | 不生成月报 |
| 图表 | `report_charts` | 是 | 这一节缺图，月报继续写 | 该节写「本节图表暂缺」 |
| 洞察 | `code_wizards`、`code_wizard` | 是 | 这一节缺洞察，月报继续写 | 该节写「本节洞察暂缺」 |
| 正文 | `report_section` | 否 | 这一节没写出来，整份月报失败 | 不生成月报 |
| 审稿、改稿 | `report_review`、`report_revise` | 否 | 整份月报失败，父 run 进入 ANSWER | 不生成月报 |

- 原因：见 INC-1。失败的那次调用是 `code_wizards`。洞察没有算出来，图表调用仍在成功，月报却因为这一次错误整条失败。
- 检查：由 `scripts/check_invariants.sh` 执行。`backend/report/` 里的 `TaskGroup` 只能有一处，文件在白名单 `[report_task_group]`。这一处是四节正文，必选步骤，一个失败取消其余。
- 和 I-6 的关系：可选步骤之间互不取消。必选步骤失败才取消其余还没发出的调用。图表和洞察不要改回一起取消。
- 可选步骤失败直接降级，不在报告内部重试。`call_model_async` 也不按 `retryable` 对同一个模型再试一次。它只在路由的 `fallback_on` 包含这个 kind 的 `fallback_group` 时换下一个候选。`unknown` 不在这张表里。供应商超时的组是 `provider`，审稿因此会换到备用模型，这不是同一调用的重试。
- 测试：洞察调用注入失败，草稿仍然生成，对应章节写着「本节洞察暂缺」，图表调用次数不少，`degraded` 里有记录。图表调用注入失败，草稿仍然生成，对应章节写着「本节图表暂缺」，洞察还在。大纲调用注入失败，report run 失败，父 run 收到 `{ok:false}` 并进入 ANSWER。`ModelBudgetExceeded` 不降级，整条 run 失败。

## I-6 失败要真正取消

- 规则：能被取消的工作只能是异步调用。同步版模型调用不保留，`to_thread` 里也就没有可打模型的函数。必选步骤里一个并发任务失败，其余还没发出的模型调用不再发出。可选步骤（图表、洞察）互不取消，见 I-5。
- 原因：见 INC-1。`_charts_and_insights_async` 写明一个失败会取消另一个，`code_wizards` 失败后图表的 6 次调用仍继续成功。取消停不掉已经进了线程的同步调用。`_call_deepseek` 里的 `_sync_invoke` 是间接调用，grep 目标函数看不到里面再调的同步函数。
- 检查：由 `scripts/check_invariants.sh` 执行。`call_model`、`_ask_ds`、`deepseek_json`、`_call_deepseek` 的同步定义必须为空，类方法的缩进也算。这条只防这几个名字回潮，不保证没有别的同步模型函数。间接调用靠「同步版已经删掉」来保证，不再单独扫 `to_thread`。
- 测试：正文写作里一个必选章节失败，其余章节的新模型调用不再发出。洞察失败不会取消图表。

## I-7 生产代码不为测试开后门

- 规则：测试通过依赖注入换实现。生产函数里不判断自己是不是被换过。
- 原因：`_next_action_async` 里的 `if _next_action is not _ORIGINAL_NEXT_ACTION` 是测试替换 `_next_action` 的后门。同一件事有一条生产路径和一条旁路，和两套模型调用是同一类问题。
- 检查：由 `scripts/check_invariants.sh` 执行。`_ORIGINAL_` 必须为空。
- 测试：生产路径不依赖被换掉的函数。测试注入替身时，替身从参数或构造函数传入。

## 检查清单

先跑 `scripts/check_invariants.sh`，把违反点列全，再改。白名单见 `docs/invariants_allowlist.txt`。命令只在脚本里。

| 规则 | 怎么算过 | 测试 |
|---|---|---|
| I-1 `asyncio.run(` | 每个入口文件一处，文件集合等于白名单的 `[asyncio.run]` | `TaskGroup` 里两次模型调用，重复几十遍 |
| I-1 `new_event_loop` | 只有白名单 `[new_event_loop]` 里的文件，每个一处 | 两个线程拿到同一个 MCP loop |
| I-1 `run_until_complete`、`_await`、`_background_loop` | 空 | 同上 |
| I-3 `fail_run` | 空 | `test_finalize_all_paths` |
| I-3 `_write_terminal` | 只定义在 `backend/db/database/run.py` | 同上 |
| I-3 直接写成终态的 `_transition(` / `transition_run(` | 只有白名单 `[terminal_transition]` | 同上 |
| I-4 `except ProviderError`、`except ModelCapacityExceeded` | 只有 `backend/resilience/errors.py` | 事件循环重入分类为 `unknown` |
| I-6 同步的 `call_model`、`_ask_ds`、`deepseek_json`、`_call_deepseek` | 空。只防这几个名字回潮 | 洞察失败后图表不再打模型 |
| I-7 `_ORIGINAL_` | 空 | 替身从参数传入 |

# 终态、通知、对账

`Outcome` 在 `backend/db/outcome.py`。`kind` 是 `succeeded`、`failed`、`cancelled`。`reason` 没有默认值。`dead_letter` 为真时，同一次 `_write_terminal` 往 `dead_letter_jobs` 写一行。时长用尽走 `over_duration_outcome`：已经请求取消则 `cancelled`，`reason` 为 `over_duration`，不写死信；否则 `failed`，错误码 `COST_BUDGET_EXCEEDED`，写死信。worker 超时和 sweeper 时长清扫用这同一个函数。

## 出口

`db/` 不 import `orchestrator/`。写状态和通知父 run 分开。

| 出口 | 位置 | 做什么 |
|---|---|---|
| `_write_terminal` | `backend/db/database/run.py` | 调用方已锁住 run 的事务里，把 `status` 写成终态。需要死信时同一事务写入 |
| `finalize_run_row` | 同上 | 自己开事务：加锁、校验起点、`lease_owner`、取消标记，再 `_write_terminal`。没写返回空 |
| `finalize_run` | `backend/services/run_finalize.py` | `finalize_run_row` 写成功后跑 `after_terminal`。没写就返回，留给 sweeper |
| `after_terminal` | 同上 | 事务外：排标题、`notify_parent`、失败时把这次对话留在会话里 |
| `after_terminal_row` | 同上 | 按已经落库的 status 组一个 `Outcome`，再跑 `after_terminal` |
| `notify_parent` | 同上 | 有 `owner_run_id` 时调用 `settle_terminal_child`。异常只记 warning |

`_ENTRY`：`succeeded` 从 `running`；`failed` 从 `running`、`waiting_for_tasks`；`cancelled` 从 `running`、`queued`、`waiting_for_user`、`waiting_for_tasks`。worker 传入这次认领的 `lease_owner`，对不上就不写。sweeper 在自己的事务里调 `_write_terminal`，不传 `lease_owner`。成功时已有 `cancel_requested_at` 则不写成功；取消时没有这个标记则不写取消。

`notify_parent` 走到 `finish_child`。checkpoint 先写入。同一 `task_id` 保留第一次；内容不同的后到结果记 `wave.result_ignored`，不改 `stale_wakes`。Redis 的波次计数只在这个 `task_id` 第一次出现时减一，减到 0 才 `requeue_waiting_tasks`。计数不存在或 `store_result` 返回 `-1` 时，checkpoint 仍在，父 run 交给下面的清扫唤醒。重复通知无害。`child_terminal_result` 把 `error_json.code` 拼进结果里的 `error` 字符串。

`requeue_waiting_tasks` 只在 `waiting_for_tasks` 且没有取消标记时把 run 改回 `queued` 并加一条 execute outbox。

## 会在提交后通知父 run 的终态

- worker 超时：`finalize_run(over_duration_outcome(...), lease_owner=这次认领)`。
- worker 成功：`Outcome(kind="succeeded", output=output)`。这次写入被取消标记拒绝时，再写 `Outcome(kind="cancelled")`。
- `_settle` 永久失败：`finalize_run`，`reason` 为 `permanent`，写死信。
- 执行次数用尽：`schedule_retry` 从 `running` 调 `_write_terminal`（`reason` 为 `dead_letter`，写死信，`attempts` 用 outbox 计数），返回 `dead_letter` 后 worker 调 `after_terminal`。
- 租约过期：`sweep_expired_leases` 从 `running` 处理。内联导入失败、已请求取消、执行次数用尽写成终态，返回这些 id；services 对每个调 `after_terminal_row`。这三条不写死信。次数还没用尽的回到 `queued`，加一条 execute outbox。
- 时长清扫：`_fail_over_duration` 覆盖 `running` 和 `waiting_for_tasks`。`queued` 不在此列，避免把还在等 worker 的 run 关掉。`max_duration_seconds` 或 `started_at` 为空则跳过。`close_attention` 对返回的 id 调 `after_terminal_row`。父 run 因此失败时，还在跑的子 run 不会被取消，各自按租约和时长收口。之后写进父 checkpoint 的结果无害：父 run 已经不是 `waiting_for_tasks`，`requeue_waiting_tasks` 返回 `None`。
- outbox 发布次数用尽：`mark_outbox_retry` 先锁 run、再锁 outbox，和 `schedule_retry`、`mark_outbox_dispatched` 同一顺序。`queued` 先到 `running`，再 `_write_terminal` 失败并写死信，`attempts` 用 outbox 计数。`dispatcher` 对返回的 id 调 `after_terminal_row`。

## 对账和空转上限

`stale_wakes` 在父 run 最新 checkpoint 的 `orchestrator` 里。

`commit_task_wave` 写 checkpoint 的同一事务里读上一份。`results` 的键和 `finished` 都没变，就沿用上一份的 `stale_wakes`；有新结果或新完成的任务则写成 0。新的 `wave_id` 不算进展。`record_child_wave_result` 写入一条新结果时也把 `stale_wakes` 写成 0。

`sweep` 里 `sweep_waiting_for_tasks` 先跑 `_repair_stale_owners`，再跑数据库里的 `sweep_waiting_for_tasks`。对象是停在 `waiting_for_tasks` 且超过阈值的父 run。

- 还有子 run 处于 `queued`、`running`、`waiting_for_tasks`：这一轮不补结果、不加计数、不唤醒。活子 run 交给租约清扫和时长清扫。
- 子 run 已终态，而 `orchestrator.results` 没有它的 `task_id`：`settle_terminal_child`。这一次抛错只记 warning，下一轮再补。
- 未完成的任务是整份 `orchestrator.tasks` 里还没有结果的 id。当前这一波的 id 在 `orchestrator.wave_ids`。整份计划在 `orchestrator.tasks`。
- 没有未完成任务：`note_stale_wake(progressed=True)` 把计数写成 0。补上缺失结果之后未完成集合变空，也走这一支，所以计数停在 0。随后数据库清扫把父 run 以 `waiting_tasks_stale` 重新入队。
- 仍有未完成任务：这一轮 `results` 的键变了则计数写成 0，否则加一。`note_stale_wake` 只在父 run 仍是 `waiting_for_tasks` 时写 checkpoint。
- 计数达到 `STALE_WAKE_LIMIT`：`_force_unfinished` 给每个缺结果的任务写入 `error` 为 `WAVE_STALE: task {id} ended without a result` 的失败结果，经 `finish_child` 同时放进 `results` 和 `finished`，再 `requeue_waiting_tasks(reason="stale_limit")`。`ready_wave` 只看 `finished`。父 run 再次进入 `execute_parent` 时，`dispatch_wave` 返回空，调用 `_answer`。
- 计数未到 `STALE_WAKE_LIMIT`：同一次清扫后面的 `sweep_waiting_for_tasks` 看到子 run 都已结束，以 `waiting_tasks_stale` 入队。

## 另外几处终态

这两处直接 `_transition`，没有父 run 需要通知：

- API 取消 `queued`、`waiting_for_user`、`waiting_for_tasks`。被取消的 `waiting_for_tasks` 父 run 自己就是终态。`running` 只写 `cancel_requested_at`，终态由持有租约的 worker 写。
- 合同内联导入成功，和合同写在同一个事务里，`_transition` 到 `succeeded`，不带 `output`。这条 run 没有父 run。它的租约过期走上面的租约清扫，失败且不写死信。

队列里卡住的 run、worker 丢失重投用尽、容量等待超时先转到 `running`（`queued` 没有到 `failed` 的边），再经 `_fail_locked_run` → `_write_terminal` 写成 `failed` 并写死信。方法返回这次新终态的 run id，提交后调用方调 `after_terminal_row`。没人认领的 run 由 `republish_unclaimed` 重新投递，不另写终态。

认领生成的 `lease_owner` 带随机后缀（`backend/celery/tasks.py` 的 `claim_owner`）。同一次执行把这个值传给 `finalize_run`，不在别处重拼。`finalize_run_row` 在调用方传入 `lease_owner` 时，这个值必须和行上的一致。
