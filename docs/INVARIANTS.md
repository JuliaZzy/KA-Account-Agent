# 不变量

不变量：无论怎么改代码都必须保持为真的规则。

让 AI 改 `backend/` 之前，先读本文件，并在回复里说明改动涉及哪几条。

## INC-1 红牛月报父 run 空转

2026-11-23 红牛月报，子 run `run_57fa43cc17e34829ba6fbf3b97db02c8`。

同一次月报里，`data.react` 三路并发都成功。`report.optional` 的 `code_wizards` 在已经跑着的事件循环上调用 `run_until_complete`，报了 `This event loop is already running`，被记成 `PROVIDER_INTERNAL`。`TaskGroup` 写明一个失败会取消另一个，图表的 6 次调用仍继续成功。子 run 失败之后没有交给父 run，父 run 空转了约 95 次。

后面的原因写「见 INC-1」。

## I-1 并发只用 asyncio

- 规则：异步代码里只 `await` 异步函数。`asyncio.run` 只允许出现在进程入口，每个入口文件一处。不保留进程内共享的事件循环，也不为它加锁。
- 原因：见 INC-1。调模型有过两套实现。`data.react` 走 `_ask_ds_async` → `deepseek_json_async` → `call_model_async`，全程 `await`，三路并发都成功。`report.optional` 走到同步的 `_call_deepseek` → `_await` → `run_until_complete`，撞上已经在跑的循环。
- 检查：由 `scripts/check_invariants.sh` 执行。每个白名单文件里 `asyncio.run(` 恰好一处，别处没有。`run_until_complete`、`new_event_loop`、`_await`、`_background_loop` 整词匹配必须为空。注释行不算命中。白名单是允许留下的入口，不是把今天的命中抄进去。
- 测试：一个 `TaskGroup` 里同时发起两次模型调用，重复跑几十遍，两次都返回，没有 `This event loop is already running`。

当前允许的入口：`backend/services/run_worker.py`（worker 执行协程）、`backend/contract/read.py`（命令行 `main`）、`backend/llm/client.py`（`if __name__` 脚本）。同一文件里的第二处算违反，白名单不给它开口子。

## I-2 互斥只靠数据库状态转换

- 规则：跨 worker 的互斥只靠数据库状态转换。认领一条 run 使用 `status == queued` 加 `with_for_update`，认领后改为 `running`，并把 `lease_owner` 写成这次认领生成的值。这个值每次认领都不同，带随机后缀，不是 `celery:{pid}` 这种进程名。同一个进程再次认领同一条 run 时，旧的 `lease_owner` 对不上。`finalize_run` 更新终态时，`WHERE` 带上这次的 `lease_owner`。更新影响 0 行，这次写入不发生。不另加版本列。
- 原因：见 INC-1。这次事故不是两个 worker 同时执行了同一个 run。父 run 的唤醒和 worker 的认领已经用状态转换：`requeue_waiting_tasks` 锁 `waiting_for_tasks`，`claim_chat_turn` 锁 `queued`，第二条消息看到的行已经不是 `queued`，直接返回。留下的口子是租约中途过期：sweeper 把 `running` 改回 `queued`，另一个进程可以再认领并再跑一遍。旧 worker 若在执行结束后仍然写终态，两个 worker 会先后各写一次。
- 检查：终态更新的语句里带 `lease_owner` 条件。新增的互斥不靠进程内的锁或全局可变对象挡住另一个 worker。
- 测试：同一 `run_id` 的两条执行消息同时认领，只有一条进入执行。同一个进程（同一个 pid）在租约过期后再次认领，旧的 `lease_owner` 写不进终态，行保持第二次认领的结果。

## I-3 终态只有一个出口

- 规则：run 置为终态只能通过 `finalize_run`。`RunStatus` 的终态是 `succeeded`、`failed`、`cancelled`，没有 `done`，也没有 `dead`。死信是 `dead_letter_jobs` 里的一行，run 仍写成 `failed`，由 `finalize_run` 写。成功、普通异常、`ProviderError`、`ModelCapacityExceeded`、取消、租约过期、死信都走它。置终态、把结果写入父 run 的 checkpoint、把父 run 的唤醒写入 outbox，在同一个事务里完成。
- 对账：sweeper 发现子 run 已终态，而父 checkpoint 的 `orchestrator.results` 没有这个 `task_id`。它调用 `finalize_run` 把结果补上。sweeper 不单独把父 run 改成 `queued`。
- 上限：连续空转的次数存在父 run 最新 checkpoint 的 `state_json.orchestrator.stale_wakes`，整数，不是 `agent_runs` 的列。sweeper 在以 `waiting_tasks_stale` 唤醒之前调用 `note_stale_wake`：这一轮没有新的子结果则加一，有新结果则写成 0。加一之后达到 3，不再入队，由 `finalize_run` 把父 run 带进 ANSWER。同一轮里先对账、再数数：补成功才归零，补失败再加一。
- 原因：见 INC-1。子 run 遇到 `ProviderError` 后直接抛出，worker 只调用了 `fail_run`，没有 `finish_child`。父 run 停在 `waiting_for_tasks`。sweeper 看到子 run 已经结束，只把父 run 重新入队，checkpoint 里仍然没有结果。父 run 再派发时命中已经失败的幂等键，又停回去。
- 检查：由 `scripts/check_invariants.sh` 执行。`fail_run` 整词匹配必须为空，不会命中 `_fail_run`。`_fail_run` 只出现在定义 `finalize_run` 的那个文件里。
- 测试：`test_finalize_all_paths`。在每个失败点注入故障（`ProviderError`、容量超限、取消、进程被杀、租约过期），断言父 run 在有限次数内进入 ANSWER。对账和上限分开断言：补成功时 checkpoint 有结果并且 `stale_wakes` 归零；补失败时计数加一，父 run 仍不入队；连续 3 次没有新结果时父 run 进入 ANSWER，状态不是 `queued`。

## I-4 错误分类只有一个函数

- 规则：所有异常经过 `backend/llm/errors.py` 里的 `classify(exc) -> ErrorKind`。本地 `RuntimeError`（包括 `This event loop is already running`）归为内部错误，不记成 `PROVIDER_INTERNAL`。重试、换模型、熔断只看 `ErrorKind`。
- 原因：见 INC-1。这次的 `RuntimeError` 被收成 `ProviderError` / `PROVIDER_INTERNAL`。供应商没有失败，本地循环被重入了。按供应商错误处理之后，这条调用不重试、不换模型，整条月报被判死。
- 检查：由 `scripts/check_invariants.sh` 执行。`except ProviderError` 和 `except ModelCapacityExceeded` 只出现在 `backend/llm/errors.py`。其他文件捕获异常只写 `except Exception as exc:`，然后 `kind = classify(exc)`。
- 测试：`This event loop is already running` 分类为内部错误，码不是 `PROVIDER_INTERNAL`，不进入供应商熔断，不触发换模型。

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
- 检查：`report_charts`、`code_wizards`、`code_wizard` 的失败路径不把整条 report run 标成 `failed`。
- 测试：洞察调用注入失败，草稿仍然生成，对应章节写着「本节洞察暂缺」。图表调用注入失败，草稿仍然生成，对应章节写着「本节图表暂缺」。大纲调用注入失败，report run 失败，父 run 收到失败结果并进入 ANSWER。

## I-6 失败要真正取消

- 规则：能被取消的工作只能是异步调用。同步版模型调用不保留，`to_thread` 里也就没有可打模型的函数。一个并发任务失败，其余还没发出的模型调用不再发出。
- 原因：见 INC-1。`_charts_and_insights_async` 写明一个失败会取消另一个，`code_wizards` 失败后图表的 6 次调用仍继续成功。取消停不掉已经进了线程的同步调用。`_call_deepseek` 里的 `_sync_invoke` 是间接调用，grep 目标函数看不到里面再调的同步函数。
- 检查：由 `scripts/check_invariants.sh` 执行。`call_model`、`_ask_ds`、`deepseek_json`、`_call_deepseek` 的同步定义必须为空，类方法的缩进也算。这条只防这几个名字回潮，不保证没有别的同步模型函数。间接调用靠「同步版已经删掉」来保证，不再单独扫 `to_thread`。
- 测试：`TaskGroup` 里洞察调用失败，图表任务不再发起新的模型调用。

## I-7 生产代码不为测试开后门

- 规则：测试通过依赖注入换实现。生产函数里不判断自己是不是被换过。
- 原因：`_next_action_async` 里的 `if _next_action is not _ORIGINAL_NEXT_ACTION` 是测试替换 `_next_action` 的后门。同一件事有一条生产路径和一条旁路，和两套模型调用是同一类问题。
- 检查：由 `scripts/check_invariants.sh` 执行。`_ORIGINAL_` 必须为空。
- 测试：生产路径不依赖被换掉的函数。测试注入替身时，替身从参数或构造函数传入。

## 检查清单

先跑 `scripts/check_invariants.sh`，把违反点列全，再改。白名单见 `docs/invariants_allowlist.txt`。命令只在脚本里。

| 规则 | 怎么算过 | 测试 |
|---|---|---|
| I-1 `asyncio.run(` | 每个白名单文件一处，文件集合等于白名单 | `TaskGroup` 里两次模型调用，重复几十遍 |
| I-1 `run_until_complete`、`new_event_loop`、`_await`、`_background_loop` | 空 | 同上 |
| I-3 `fail_run` | 空 | `test_finalize_all_paths` |
| I-3 `_fail_run` | 只有定义 `finalize_run` 的文件 | 同上 |
| I-4 `except ProviderError`、`except ModelCapacityExceeded` | 只有 `backend/llm/errors.py` | 事件循环重入分类为内部错误 |
| I-6 同步的 `call_model`、`_ask_ds`、`deepseek_json`、`_call_deepseek` | 空。只防这几个名字回潮 | 洞察失败后图表不再打模型 |
| I-7 `_ORIGINAL_` | 空 | 替身从参数传入 |
