# 聊天回合：计划、入队、ANSWER

改编排器、`run_loop`、Celery 投递或会话锁之前先读这份。`docs/run.md` 讲租约、事件、重跑和清扫。本文件只讲**这一轮对话**怎么拆成任务、怎么进 worker、怎么回到用户看得到的那一句。

`docs/agent-call-paths.html` 里这两句作废，以这里为准：

- 「计划为空才进工具循环」
- 「只有一个代理时，由它自己回复」

旧代码没有 Celery，计划在进程里用线程池跑完。新代码已经有 Redis + outbox + Celery，并行交给**不同 worker 领不同子 run**，不要再在一个 worker 里开线程池。

聊天路径已经按这个骨架接上。`parent_run_id` **只表示 rerun**（`POST /runs/{id}/rerun`，新行指向旧行），不要拿它当「谁派了这个工具」。调度边是 `owner_run_id`。

## 目标骨架

```
用户一句话
  ├─ OPEN（仅无标题的新会话；独立 run，后台改 tags/title，不进 tasks，不向聊天 SSE 推事件）
  └─ 父 run（run_type=chat，带着 session_id）
        → PLAN（官方 qwen3.7-flash）
        → tasks[]，每项 id / kind / name / input / depends_on
        → 空 depends_on 的同一波：每项一条子 run，outbox 投递
        → 任意空闲 worker 领子 run（带 session_id + owner_run_id）
        → 子 run 还要再调 agent/tool：再入队，owner 指这个子 run
        → 结果按 task id 写入 Redis 回合箱
        → 这一波计数归零：把父 run 重新入队
        → 下一波，或全部结束
        → 无论是否跑过任务，材料 + 用户原话交给 ANSWER
        → save_reply，松开会话写入锁
```

排队的粒度是**任务**，不是 session。消息带 `session_id`、`owner_run_id`、`task_id`。Worker 认任务，不认「这个对话归我了」。

## 列和状态

### `parent_run_id` 不动

继续只表示「这条是那条的重跑」。监控、幂等、`docs/run.md` 的重跑表都不改含义。

### `owner_run_id`

调度边。谁在这一轮派出了我：

- 聊天父 run 派出的 tool/agent 子 run：`owner_run_id = 父 chat run`
- 子 run 自己再派的（报表要查数）：`owner_run_id = 那个报表 run`

可空。重跑出来的新行：`parent_run_id` 指向旧行，`owner_run_id` 按新这一轮重新派。

页面仍订阅父 chat run 的事件流。子 run 的 `tool.started` / `tool.completed` 要**投影**到父 run（或写一份带 `owner_run_id` 的公开事件），桌面不用改成跟一串 id。

### 状态 `waiting_for_tasks`

父 run（以及会再派活的子 agent）派完一波之后**放掉租约**，进入 `waiting_for_tasks`。不要占着 worker 轮询，也不要在任务里对别的 Celery 任务 `.get()`（官方 canvas 文档也禁止这样做，prefork 会自己卡死）。

状态机在现有六个之外加这一态：

```
queued → running → waiting_for_tasks → queued（波次到齐，checkpoint 指向下一波或 ANSWER）
                 → waiting_for_user
                 → succeeded / failed / cancelled
```

`waiting_for_user` 仍只表示等人。清扫：子 run 租约过期按 `docs/run.md` 重入队；父 run 在 `waiting_for_tasks` 超时（子全失败或计数丢了）则失败或重派未完成的那一波，不要当成「等人」。

### Redis 回合箱

Redis 只做这一波的计数和暂存，**账本在 Postgres**：子 run 的 `agent_steps` / `tool_calls` / `model_calls` / `output_json`，以及 owner 的 `checkpoints.results`。不要拿 Redis 当唯一结果，也不要换成 Celery chord。

- `begin_turn(session_id, turn_id, task_ids)` 给这一波上计数
- 每个子 run 结束：先落库（step + tool_call + 写进 owner checkpoint），再 `store_result(...)`，返回还剩几项
- 返回 `0` 的那一个负责把 owner 重新 `execute_run` 入队
- `collect_wave` 合并 checkpoint、子女 `output_json` 和 Redis；Redis 丢了仍能从库里恢复

`turn_id` 是这一**波**的 id，不是整个 chat run。下一波新开一个 turn。

## 四个模型步骤

### OPEN

只给还没有标题的会话打 `tags` + `title`。不是代理，PLAN 不调度它。入队聊天父 run 的同时另建一条 `run_type=open`，和 PLAN 并行。失败留空标题，不挡这一轮。事件只写在这条 OPEN run 上，不投影到父 chat run，聊天页 SSE 看不到它。

提示词仍是 `backend/orchestrator/prompt.py` 的 `OPEN`。

### PLAN

父 chat run 领到租约后的第一件业务事。用官方 `qwen3.7-flash`（`qwen_json`，带 schema，思考关闭）。只输出要执行的任务，不跟经销商说话。系统提示带上 standing 备忘和会话滚动摘要（`attach`），和 ANSWER 同一份，只作背景。

目标 schema（和删掉之前、以及现有测试夹具一致）：

```json
{
  "tasks": [
    {
      "id": "string",
      "kind": "agent | tool",
      "name": "string",
      "input": {},
      "depends_on": ["string"]
    }
  ]
}
```

规则：

- `kind=agent` 的 `name` 只能是 `po` / `contract` / `report` / `brand` / `data`。代理已经收成工具，运行时对到 `po.draft`、`contract.read`、`report.draft`、`brand.search`、`data.query`。
- `kind=tool` 的 `name` 是原生或 MCP 函数名（`web_search`、`translate`、`exchange_rate`、`send_email`，以及启用的 MCP）。
- 没有依赖：`depends_on` 为空，同一波并行入队。
- 只有下一步要用上一步的结果时才填 `depends_on`。translate 的正文、send_email 的 body **运行时从上一波填**，PLAN 的 `input` 里不要编占位句。
- 写报表只排 `report`，不要为同一份草稿再排 `data`。report 自己要查数时，由那个子 run 再入队。
- 会话里已有未完成的采购单时，计划里最多保留一个 `po`。
- 寒暄、澄清、或这一轮什么都不用跑：`tasks` 为空数组。空计划**不**进工具循环，直接 ANSWER。
- 不要编造 SKU、价格、日期、国家。`input` 只放用户说过的。

编排 system 提示在 `backend/orchestrator/prompt.py` 的 `plan_system()`（内含 `all_schemas()` 的 tool JSON）；schema 在 `plan_schema()`。聊天路径走官方 `qwen_json`，不走校园 `minimax`，也不走官方 DeepSeek。

聊天父 run **不再**用 `REACT` 选工具。`REACT` 从父 run 的 decide 上拿掉。子 agent 若还要再拆任务，走自己的 PLAN（或固定步骤），不要在父 run 里开 ReAct 循环。

### 子 run

一条任务一条 `agent_run`，走现有 outbox → `execute_run`：

| 计划 name | `run_type` | 执行 |
|---|---|---|
| `po` | `po` | 现有 `po.draft` 路径 |
| `contract` | `contract` | `contract.read` |
| `report` | `report` | `report.draft`；内部再查数就再入队 `data` |
| `data` | `data` | `data.query` |
| `brand` | `brand` | `brand.search` |
| 原生 / MCP | `tool`（`RunType` 补一个） | 现有 `backend/tools` / MCP runner |

子 run 的 `input_json` 至少有：`session_id`、`owner_run_id`、`task_id`、`name`、`input`、上一波依赖结果。幂等键按 `chat:{session_id}:{owner_run_id}:{task_id}` 这类带任务身份的键，不要用父 run 那一把。

子 run **不**拿 `hold_session`，**不**写会话 `history`。写草稿、查数、检索都可以，对用户开口只发生在父 run 的 ANSWER + `save_reply`。

`claim_chat_turn` 的「同一 session 只能有一条 running chat」继续成立，且**只约束 `run_type=chat`**。子 run 不是 chat，可以和父 run 的 `waiting_for_tasks` 同时存在，也可以彼此并行。

### 嵌套入队

报表要查数、翻译要等检索、发信要等译文：需要就再排一条子 run，`owner_run_id` 指当前这条。当前这条进入 `waiting_for_tasks`，计数归零后再继续自己的步骤，最后把 payload 写回自己 owner 的回合箱。

不要在子 run 进程里直接函数调用另一个 agent（例如 `report.draft` 里同步 `answer_question()`），除非那一步明确是纯代码、没有第二段模型、也没有独立租约价值。默认：再入队。

### ANSWER

**用户看得到的回复只出自这里。** 包括：

- `tasks` 为空（你好、澄清、用不上工具）
- 只跑了工具
- 只跑了代理
- 代理 + 工具混在一起

父 run 把这些拼进模型输入，再调 `ANSWER`（校园 `deepseek-chat`）：

1. 用户原话
2. 最近对话（fold 过的）
3. 记忆块（standing / summary），和 PLAN 同一份 `attach`
4. 这一轮全部任务结果（成功、失败、跳过），按 task id，不要丢

提示词在 `backend/orchestrator/prompt.py` 的 `ANSWER`。不要再恢复单独的 `SYNTHESIZE`：空计划和有结果走同一段。

采购单价格、SKU、MOQ、规则仍由 `backend/po/` 算。ANSWER 只引用结果，不算总价，不宣布已经下单。

## 锁和并行

| 锁 | 管什么 | 不管什么 |
|---|---|---|
| `claim_chat_turn` | 同一 session 同时只有一条 **chat** 父 run 在 `running` / `waiting_for_tasks` | 子 run |
| `hold_session` | `save_reply`、确认下单、和生成抢同一行会话的那一下 | 工具执行、PLAN、子 run |
| 子 run 租约 | 这一条任务 | 整个对话 |

两句用户消息仍然排队：第二条 chat run 留在 `queued`，等第一条终态（或等人）。这是对话写入的排他，不是工具的排他。

**聊天路径禁止 `ThreadPoolExecutor`。** 并行 = 同一波多条 outbox。Celery `--concurrency` 和多 worker 进程就是并行度。进程内线程只留给工具自己必须的短超时守护（现有 `timeout_seconds` 那种），不要用来跑另一条业务任务。

## 代码在哪

| 文件 | 职责 |
|---|---|
| `backend/orchestrator/prompt.py` | `plan_system()` / `ANSWER`。父 run 不用 `REACT` |
| `backend/orchestrator/plan.py` | schema、解析、`scheduled`、`ready_wave`、`make_plan` |
| `backend/orchestrator/gather.py` | 按波入队、`begin_turn`、建子 run、投影 `tool.started` / `tool.completed`、唤醒 owner |
| `backend/orchestrator/parent.py` | 父 run：PLAN → 按波入队 → ANSWER |
| `backend/orchestrator/report.py` | 报表子 run：规划后入队 data，恢复后续撰写 |
| `backend/orchestrator/turn.py` | `execute_open`；`run_tool_calls` 只串行，聊天父 run 不再走这里 |
| `backend/services/chat_run.py` | 入队时同时建 OPEN；`execute_chat`：open / 父 PLAN / 子任务。`hold_session` 只包会话写入 |
| `backend/services/task_run.py` | 一条子 run 调一个工具，写 `tool_calls` + step，再进回合箱 |
| `backend/services/run_loop.py` | 留给**单条**工具内部的占座、幂等、超时。不要再在这里并行一整波 |
| `backend/services/session_tools.py` | 工具实现。父 run 按计划名字调度它们，而不是 `tools=` 让模型点 |
| `backend/db/orm/run.py` + `0024_run_owner` | 可空 `owner_run_id` |
| `backend/redis_store.py` | 一波任务的结果箱，不是线程池邮箱 |
| `backend/celery/tasks.py` | 仍是一条 `execute_run(run_id)`。不要上 Celery chord |

嵌套入队已经接上：报表规划出问题后把每条查数排成 `data` 子 run（`owner_run_id` 指向报表 run），报表自己进入 `waiting_for_tasks`。风险补查是第二波。直接调用 `iter_draft` 的测试和单测仍走进程内 `answer_question()`。

子 run 的 `model.*` / `step.*` 会沿 `owner_run_id` 投影到父 run（payload 带 `projected_from`），聊天页继续只订父 chat run。工具起止仍由 `gather` 写成父 run 的 `tool.started` / `tool.completed`。

`sweep_waiting_for_tasks`：owner 停在 `waiting_for_tasks` 超过 15 分钟，且子女都已终态（或没有子女），就重新入队。不要当成 `waiting_for_user`。子女还在 queued / running 时不动。

## 不要做的

- 不要用 `parent_run_id` 表示调度父子。
- 不要在 Celery 任务里 `ThreadPoolExecutor` 跑另一条业务任务，也不要 `group/chord.get()`。
- 不要让子 run 写 `history` 或走 `save_reply`。
- 不要让 ANSWER 算价格或宣布已下单。
- 不要把「计划为空」重新接回 ReAct 工具循环。漏排的任务是 PLAN 的错，下一轮再排，不要藏第二套选工具逻辑。
- 不要让一个 session 的工具独占一个 worker 直到整轮结束。
- 不要在 `.env` 里写库地址；不要在宿主机起 Postgres / Redis / 用系统 Python。命令走 `./dev`。

## 改完之后怎样算对

一句话里同时问订单总额、天气、汇率、再译成德文：

1. PLAN 给出查数 / 检索 / 汇率（无依赖）和 translate（`depends_on` 检索）。
2. 前三个是三条子 run，两个以上 worker 时不应排成一条队串完。
3. 检索进 Redis 之后，translate 才入队，正文是检索结果，不是模型编的。
4. 父 run 在等波次时是 `waiting_for_tasks`，租约已释放。
5. 只有一条助手消息，来自 ANSWER，数字能对上子 run 结果，失败的项如实说没拿到。

「你好」：PLAN 返回 `[]`，ANSWER 直接短答，不点工具，不进 `run_loop` 的工具占座。
