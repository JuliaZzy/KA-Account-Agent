# 聊天一轮：从发消息到回复（叙述版）

这是一份**按时间顺序**讲清「用户发了一条聊天消息后发生了什么」的文档，适合 onboarding 或对外讲解。租约、清扫、重试、监控等细节见 [run.md](run.md)；PLAN schema、波次规则和 `owner_run_id` 见 [orchestrator.md](orchestrator.md)。图见 [agent-call-paths.html](agent-call-paths.html)。

## 角色与表

一次用户动作对应一条 **run**（`agent_runs` 一行）。**轨迹和对话分开**：给人看的对话在 `chat_sessions.history`（JSON 列，不是独立表）；run 轨迹在 Postgres 里按表记。

| 表 | 这一轮里干什么 |
|---|---|
| `agent_runs` | **当前状态**（同一 `run_id` 原地更新）、输入输出、租约、`owner_run_id` / `parent_run_id` |
| `run_events` | **只追加**的时间线事实，`sequence` 递增；驱动 SSE（`NOTIFY run_events`） |
| `agent_steps` | **一步**模型或一步工具的账本（`step_index`、input/output、起止时间）；`model_calls` / `tool_calls` 挂 `step_id` |
| `outbox_jobs` | 待 Celery 执行的 `execute_run`（`pending` → `dispatched`） |
| `checkpoints` | 编排状态与断点续跑（任务图、`wave_id`、已收集结果等） |
| `model_calls` / `tool_calls` / `usage_records` | 每次模型/工具调用的明细与用量 |
| `audit_logs` | 状态迁移审计（随 `_transition` 写入） |
| `chat_sessions` | 会话元数据；**本轮结束**时 `history` 追加 user/assistant（带 `run_id`） |

**`agent_steps` 和 `run_events` 不重复：** steps 给执行与 `/trace` 恢复用（完整 JSON）；events 给 UI 进度与流式订阅用（带 `display`、公开/内部分流）。开 step 会写 `step.started` 事件，关 step 会写 `step.completed` 并更新 step 行。

**调度父子**看 `owner_run_id`（谁派了这条子 run）。**重跑**看 `parent_run_id`，不是波次父子。

Redis 在这里有三件事，不要混：

- **Celery broker**（常见配置下 Redis 1 号库）：`execute_run` 消息排队等 worker 领。
- **波次回合箱**（`begin_turn` / `store_result`）：只做**这一波的计数和结果暂存**；权威结果仍在 Postgres（子 run 输出 + owner checkpoint）。**减到 0 不会主动通知 Celery**；最后一个子 run 的 worker 在代码里调 `requeue_waiting_tasks` + `dispatch_pending`。
- **临时标题标记**（`mark_provisional`）：首条消息写 title 时的辅助键，与波次无关。

## 总览

```
用户 POST /message
  → [可选] PG(chat_sessions.title) + Redis 临时标题
  → 事务：agent_runs(queued) + run.created + outbox(pending)
  → dispatch：broker 发消息 → 另一事务：outbox(dispatched) + run.enqueued（内部）
  → worker 事务：claim → running + run.started + audit_logs
  → 父 chat run：PLAN（多笔 PG：step / model / checkpoint）→
       有任务：事务 commit_task_wave → Redis begin_turn → dispatch 子 runs
       → 子 run：多笔 PG → finish_child 合并父 checkpoint → Redis dec
       → left==0：父 waiting_for_tasks → queued + 新 outbox → 再 claim → 下一波或 ANSWER
  → save_reply：PG(chat_sessions.history)（持 Redis 会话锁）→ 父 succeeded
  → 前端 SSE 只订父 run；子 step/model 投影到父；正文流式 run.reply_updated
```

同一 **session** 同时只有一条 **chat 父 run** 在 `running` 或 `waiting_for_tasks`；下一条用户消息可以先进 `queued`，等上一条结束。这是对话写入的排他，不是工具的排他。

---

## 第一步：HTTP 建 run

用户发送内容后，API 调用 `enqueue_chat_run`（`backend/services/chat_run.py`）。

**若无会话标题**，会**先**（与建 run **分开的事务**）更新 `chat_sessions.title`，并在 Redis 记临时标题；不调模型。

**建 run —— 同一 Postgres 事务**（`create_agent_run` / `_insert_queued_run`）：

1. **`agent_runs`**：新建一行，`status = queued`（此处是 **INSERT**，不对已有行 `FOR UPDATE`）。
2. **`run_events`**：追加 `run.created`（聊天时间线可见）。
3. **`outbox_jobs`**：`execute_run`，`status = pending`，载荷里带 `run_id`、`trace_id`。

用户本条文字在 **`agent_runs.input_json`**，此时**不**写入 `history`。`run.created` **不保证** Celery 已收到任务（见 [run.md](run.md)）。

紧随其后的 `dispatch_pending` 见下一步。

---

## 第二步：投递（dispatch）

`dispatch_pending` 对每个到期的 `pending` outbox **分两阶段**（故意不在一个事务里，见 [run.md](run.md)）：

1. **事务外**：`execute_run.apply_async` 把消息写入 **Celery broker**（聊天队列 `ka_account`；OPEN 标题精修走 `ka_account.title`）。
2. **Postgres 事务**（`mark_outbox_dispatched`）：
   - 对 **`agent_runs` 行 `SELECT … FOR UPDATE`（仅加锁，不改 `status`）**，再锁 outbox 行，避免与 worker 改状态死锁；
   - **`outbox_jobs`**：`pending` → `dispatched`，写 `dispatch_at`；
   - **`run_events`**：追加内部事件 `run.enqueued`（**不进聊天 SSE**）。

此阶段 **`agent_runs.status` 仍为 `queued`**。中间崩溃时 **sweeper** 再扫 `pending` outbox；重复投递由租约、checkpoint、幂等键挡住。

HTTP 路径在建 run 后一般会**立刻**调一次 `dispatch_pending`；sweeper 也会周期性再扫。

---

## 第三步：worker 领任务

Celery worker 拿到 `run_id`，在 `work_once` 里（`claim_chat_turn` **一个事务**）：

1. 锁 `agent_runs`（`queued`）；**chat 父 run** 还会锁 `chat_sessions` 并检查同 session 是否已有另一条 chat 在 `running` / `waiting_for_tasks`。
2. 并发闸门（Redis/配置）在 claim **之外**尝试占座，满员则 SoftBusy 重试，run 保持 `queued`。
3. **`agent_runs`**：`queued` → `running`，写租约、`started_at`、`attempts`。
4. **`run_events`**：公开 `run.started`；**`audit_logs`**：状态迁移审计。

然后进入 `execute_chat` → 父 run 走 `execute_parent`，子 run 走 `execute_task`。

**`run.started` 只出现在 claim，不出现在 dispatch。**

---

## 第四步：PLAN（仍在同一条父 run 上）

父 run **不会**为 PLAN 再建一条 run。第一件业务事是 **PLAN**（`orchestrator/parent.py` + `plan.py`）：

- 输入：用户本条（来自 run input）、`chat_sessions.history` / 记忆（standing、summary）、`plan_system()`。
- 输出 JSON：`tasks[]`（`id`、`kind`、`name`、`input`、`depends_on`）；**不需要工具**时为 **`[]`**，不是 `{}`。

落库是 **多笔事务**（不是一整块），典型顺序：

| 顺序 | 事务里写什么 | SSE（若公开） |
|---|---|---|
| `insert_step` | `agent_steps` 开步 + `run_events` `step.started` | 是 |
| `model.started` | 常单独 `append_run_event` | 是 |
| 模型返回 | `model_calls`、`usage_records`、`model.completed` 等 | 是 |
| `finish_step` | 关 step + **`checkpoints`**（`phase: planned`，任务图）+ `step.completed` | 是 |

PLAN 失败可走重试；细节见 [retry.md](retry.md)。

---

## 第五步：空计划 —— 直接 ANSWER

`tasks` 为空时，**同一次父 run claim** 内走 ANSWER（`compose_answer`），不建子 run、不进 `waiting_for_tasks`。

- 流式：`note_running_reply` 多次更新 `output_json` + `run.reply_updated`。
- 结束：`finish_step`（可带 answer checkpoint）→ `save_reply` 写 **`chat_sessions.history`** → `complete_run` → `succeeded`。

用户可见回复**只出自父 run 的 ANSWER**。

---

## 第六步：有任务 —— 按波派子 run

`ready_wave` 算出依赖已满足、尚未执行的一波任务（可并行）。

**派波之前**：Redis `begin_turn(session_id, wave_id, task_ids)` 设计数 **N**（在 `commit_task_wave` **提交之前**调用，避免极快子 run 写入时计数不存在；若 commit 失败会 `drop_turn`）。

**`commit_task_wave` —— 一个 Postgres 事务**：

1. 每个新子任务 → **子 run** 三元组：`agent_runs` + `run.created` + `outbox pending`（幂等键已存在则复用行、**不重复 outbox**）。
2. **父 run** 上每条计划任务一条 **`tool.started`**（调度语义：「这一波要跑这些」；写在**父** `run_id` 上，不是等子 worker 启动才写）。
3. **父 `checkpoints`**：任务图、`finished`、`wave_id` 等（**子 run 尚未执行**就会写）。
4. **父 `agent_runs`**：`running` → `waiting_for_tasks` + 公开 `run.waiting_for_tasks` + `audit_logs`；**释放租约**。

**事务提交之后**：

- `dispatch_pending`：每个子 run 重复「broker + `dispatched` + `run.enqueued`」（事件在**子** run 上，聊天页只订父 run 时通常看不到子的 `run.enqueued`）。

子 run `input_json` 带 `session_id`、`owner_run_id`、`task_id`、`wave_id`、依赖结果。**子 run 不写 `history`，不拿 `hold_session`。**

父 worker 本次 `execute_chat` 看到 `waiting_for_tasks` 后 **不** `complete_run`，任务结束并 ack。

---

## 第七步：子 run 执行

子 run 被任意 worker claim 后（`task_run.execute_task` 或 `report` 等代理路径），同样是 **多笔事务**，与父 PLAN 类似的原语（`insert_step`、`insert_tool_call`、`finish_step` 等），但**业务入口不同**。

典型顺序：

1. **Claim**：子 run `run.started` + `audit_logs`（同第三步）。
2. **`insert_step`**：子 run 上 `agent_steps` + `step.started`；若类型在 `PROJECTED_CHILD_EVENTS` 内，**再追加一条到父 run**（`projected_from` / `child_run_id`）。
3. 工具或代理逻辑：`tool_calls`（+ 可选子 run **`checkpoint`** `phase: tool_done`，**仅成功**）+ 子 run 上 `tool.completed` / `step.completed` 等 —— **一笔 `insert_tool_call` 事务**（或与 `finish_step` 合并）。
4. **`finish_child`** → **`record_child_wave_result`（一笔事务）**：合并 **父 `checkpoints`**（`results` / `finished`）+ 父 run 上 **`tool.completed` 或 `tool.failed`**（按 task_id）。  
   - 与上一步 **刻意分开**：子结果须先在本 run 账本落库，父再合并；且常跨 worker 进程。
5. **`store_result`**（Redis）：计数减 1，结果写入波次 hash。
6. 子 run **`complete_run`**：`succeeded` + `output_json` + `run.completed`。

**注意：** 父上的 **`tool.started`** 在第六步 `commit_task_wave` 已写好；第七步是在父上补 **`tool.completed`**，在子上写 **step / model / tool** 明细。子的 `model.*` / `step.*` **投影**到父，是为 **只订一条父 run SSE** 也能看到子任务进度，并方便 `/trace`；权威明细仍在子 run 的 steps / tool_calls。

嵌套代理（如报表再查数）再入队子 run，`owner_run_id` 指向当前代理 run。

---

## 第八步：波次到齐 —— 唤醒父 run

每个子 run 在 `store_result` 后 Redis 计数减 1。**最后一个**读到 `left == 0` 的子 worker（不是 Redis 回调 broker）在同一逻辑里：

1. **`requeue_waiting_tasks`（一笔事务）**：父 `waiting_for_tasks` → **`queued`**；`run.resumed`（内部）；**新建** `outbox pending`（不是把旧 dispatched 改回 pending）；`audit_logs`。
2. **`dispatch_pending`**：父 run 再进 Celery。
3. **`drop_turn`**：删掉本波 Redis 计数/结果键（唤醒路径）。

父 run **再次 claim** → `running` → `execute_parent`：读 **checkpoint**，`collect_wave` 合并 Redis 与库里的子结果，更新 `finished`。

- **还有下一波**（例如 4、5 依赖 1–3）→ 重复第六、七、八步。
- **没有未完成任务** → 父 run 上 **ANSWER**（同第五步的流式与 checkpoint）→ **`save_reply`** → **`succeeded`**。

Redis 计数丢失时，sweeper 仍可根据父 `waiting_for_tasks` 与子 run 终态把父拉回队列（见 [run.md](run.md)）。

---

## 第九步：写入会话与前端

### `history` 何时写

编排聊天路径下，**用户可见的一轮对话**在父 run **ANSWER 完成之后**、`complete_run` 前后，由 **`save_reply`** 调用 **`record_history`**：

- 更新 **`chat_sessions.history`**：在原有 transcript 上追加 **本轮 user + assistant**（assistant 带 `run_id` 及 desk 字段）。
- 持 **`hold_session`**（Redis 会话锁），与确认下单等路径互斥。

**入队时不写 history**；子 run 全程不写 history。确认 PO/合同/报表等其它 API 也会 `record_history`，不限于聊天 ANSWER。

### SSE

聊天页只订阅 **父 chat run** 的 `run_id`：

- `GET /runs/{id}/events/stream`：先推已落库行，再 `LISTEN run_events`（15s 心跳）。
- **公开事件**见 [run.md](run.md) 与 `PUBLIC_RUN_EVENTS`。
- **内部事件**（`run.enqueued`、`run.resumed` 等）只在 `/trace` 或监控里看。
- 流式正文：**`run.reply_updated`** + `agent_runs.output_json.text`。

关页或断 SSE **不取消** run；点停止才 `cancel`。

---

## 事件与状态：容易说混的两条线

| 你说「排队」时可能指 | 实际是什么 |
|---|---|
| `agent_runs.status = queued` | 在等 worker **领租约** |
| outbox `pending` | 在等 **dispatch** 或尚未标记 `dispatched` |
| outbox `dispatched` | 消息已发 broker；**run 仍可能是 `queued`** |
| Celery 队列里有消息 | broker 排队，与 outbox 状态、run 状态三者不同步属正常 |
| `run.enqueued` 事件 | outbox 已 `dispatched`；**内部**，聊天条不展示 |
| `run.started` 事件 | worker 已 claim，**running** |

父 run 等子任务时是 **`waiting_for_tasks`**（租约已释放），不是 `queued`。

---

## Checkpoint 何时写（编排路径）

| 时机 | 内容 | 说明 |
|---|---|---|
| PLAN `finish_step` 成功 | `phase: planned` + 任务图 | 续跑可跳过重复 PLAN |
| `commit_task_wave` | 波次 `wave_id`、图、`results` 快照 | **子 run 还没跑** |
| 子 tool 成功 `insert_tool_call` | 子 run `phase: tool_done` | 失败通常不写 |
| `record_child_wave_result` | 父 orchestrator 合并子结果 | 与父 `tool.completed` 同事务 |
| ANSWER `finish_step` | `phase: answer_done` | 含最终回复文本 |

不是「只有某次 LLM/tool 成功才写 checkpoint」；**派波停车**也会写父 checkpoint。

---

## 父 run 终态之后（与主流程并行）

父 chat 终态后，若标题仍临时，再建 `run_type=open`，投 **`ka_account.title`** 队列精修 tags/title。OPEN 事件**不**进聊天 SSE。见 [orchestrator.md](orchestrator.md) OPEN 一节。

---

## 一致性：事务边界（摘要）

| 阶段 | 通常同一事务内的表 |
|---|---|
| 建 run | `agent_runs`、`run.created`、`outbox pending` |
| 标记 dispatched | **锁** `agent_runs`、改 `outbox`、`run.enqueued` |
| claim | `agent_runs`、`run.started`、`audit_logs`（+ chat 锁 session） |
| `commit_task_wave` | 子 run 三元组（×N）、父 `tool.started`（×N）、父 checkpoint、父 `waiting_for_tasks` |
| `insert_tool_call`（子） | `tool_calls`、关 step、子 checkpoint、子 tool 事件 |
| `record_child_wave_result` | 父 checkpoint 合并、父 `tool.completed` |
| 唤醒父 | 父 `queued`、新 outbox、`run.resumed`、`audit_logs` |
| `_transition` 任意状态迁移 | `agent_runs`、`run_events`（对应类型）、`audit_logs`；若回到 `queued` 且来自 `waiting_*`，常 **+ 新 outbox** |

**故意分开的：** broker 发布 vs `dispatched` 标记；Redis `begin_turn` vs `commit_task_wave`；子工具账本 vs 父结果合并；多次 `append_run_event`（如 `model.started`）。

Redis 波次计数只决定**哪个 worker 去调** `requeue_waiting_tasks`；权威状态与结果以 Postgres 为准。

---

## 和实现文件的对照

| 阶段 | 主要代码 |
|---|---|
| 建 run + dispatch | `chat_run.enqueue_chat_run`，`db.create_agent_run`，`dispatcher.dispatch_pending`，`queue.mark_outbox_dispatched` |
| worker 入口 | `celery/tasks.execute_run`，`run_worker.work_once`，`claim_chat_turn` |
| 父 run PLAN / ANSWER | `orchestrator/parent.py`，`orchestrator/plan.py` |
| 按波入队 / 唤醒 | `orchestrator/gather.py`（`commit_task_wave`、`finish_child`） |
| 子 run 工具 | `services/task_run.py` |
| 波次 Redis | `redis_store`（`begin_turn` / `store_result` / `drop_turn`） |
| 写 history | `orchestrator/speak.save_reply`，`memory.record_history` |
| 投影事件集合 | `services/run_names.PROJECTED_CHILD_EVENTS`，`db._append_event` |
| 公开事件集合 | `services/run_names.PUBLIC_RUN_EVENTS` |

需要改编排或投递行为前，请先读各模块顶部 docstring 与 [orchestrator.md](orchestrator.md)，避免和租约、幂等、会话锁冲突。