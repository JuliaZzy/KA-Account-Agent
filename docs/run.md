# Run 运行时

一次用户动作对应一条 run。HTTP 只负责把这条 run 写进数据库并交给队列，模型和工具在 worker 里跑。页面看到的进度来自已经提交的事件。关掉页面只停本地的事件流，worker 继续跑。

目标是：可监控、可重试、中断之后能接着做，副作用不丢、也不发第二次。

采购单的价格、SKU、MOQ 和商业规则由 `backend/po/` 计算。模型只把对话填进固定槽位。建单前先烧掉 confirm token。业务数据按表、列和外键存放。

## 一次消息

```
POST /message
  → create_agent_run          同一事务：agent_runs + run.created + outbox execute_run
  → dispatch_pending          把到期的 outbox 交给 Celery
  → 这条 SSE 只交出 run_id    到这里结束

Celery execute_run（测试里直接调 work_chat）
  → 领 agent_runs 的租约，状态改为 running
  → run_loop
       要确认     → waiting_for_user
       用户点停止 → cancelled
       执行耗尽   → failed，并写入 dead_letter_jobs
       正常结束   → save_reply，succeeded

页面
  → GET /runs/{id}
  → GET /runs/{id}/events?after=0
  → GET /runs/{id}/events/stream?after={最后一条 sequence}
  → 终态之后读会话 history
  → 点停止走取消接口
```

建 run 在 `backend/services/chat_run.py` 的 `enqueue_chat_run`，事务在 `create_agent_run`。worker 入口是 `backend/celery/worker.py`，任务是 `execute_run`，循环是 `backend/services/run_loop.py`。

发出 Celery 消息和把 outbox 标成 `dispatched` 是两个事务。中间崩溃时，清扫会再投一次。挡住重复执行的是后面的租约、checkpoint 和幂等键。

## 状态

六个状态，只在 `backend/db/database/run.py` 的 `_transition` 里切换：

```
queued → running → waiting_for_user
                 → succeeded
                 → failed
                 → cancelled
```

`queued` 也可以直接到 `cancelled`。`waiting_for_user` 可以回到 `queued`（用户确认或跳过之后重新投递），也可以取消。`failed` 不能改回 `running`。名字在 `backend/services/run_names.py`，转移表在 `backend/db/database/support.py`。

工具函数不写 `run.status`。循环只在需要确认时调用 `mark_waiting_for_user`。领跑、成功、失败、取消、重试和死信由 `backend/services/run_worker.py` 经 `_transition` 完成。

同一次提交里写状态、公开事件和需要的 `audit_logs`。`_append_event` 先 `SELECT … FOR UPDATE` 锁住这条 run，再取 `max(sequence)`。同一事务 `NOTIFY run_events`，载荷只有 `run_id`。通知失败不回滚事件。

## 记在哪

轨迹和对话分开。会话 `history` 是给人看的消息，助手回复上带这次的 `run_id`，用来回拉时间线。轨迹在这些表里（`backend/db/orm/run.py`）：

| 表 | 记什么 |
|---|---|
| `agent_runs` | 状态、输入输出、租约、`trace_id`、次数和时长上限、`parent_run_id` |
| `agent_steps` | 一轮模型或一次工具 |
| `run_events` | 按 `sequence` 递增的事实，`UNIQUE (run_id, sequence)` |
| `checkpoints` | 循环中断后从哪继续 |
| `model_calls` / `usage_records` | 一次模型调用和它的用量 |
| `tool_calls` | 一次工具调用，副作用还有业务幂等键 |
| `audit_logs` | 副作用和人工修复 |
| `outbox_jobs` | 待投递的 `execute_run` |
| `dead_letter_jobs` | 执行次数用尽的任务 |

`agent_runs`、`usage_records`、`audit_logs`、`tool_calls` 的 `org_id` 指向 `distributors.id`。一家经销商就是一个 org。报表、合同、会话仍挂在 partnership 上，查某家经销商时用 `partnership.distributor_id`。

Redis 0 号库是会话锁、限流和并发计数，1 号库是 Celery broker，2 号库是任务结果。

## 身份

Run 接口只认 `X-User-Id`。用它查出 `users` 行，`org_id` 取 `distributor_id`。没有这个人返回 401 `UNAUTHENTICATED`。请求头、query、body 里的 org 不参与判断。解析集中在 `get_current_user`。登录和 httpOnly cookie 还没有；有了之后，只换这一处的用户 id 来源。

`send_email` 和需要幂等的 MCP，在调用前再核对这个用户属于这条 run 的经销商。对不上记 `TOOL_PERMISSION_DENIED`，整条 run 失败。模型点了发信，不等于这个用户有权发信。

## 事件流

`GET /runs/{id}/events?after=` 返回 `sequence` 大于 `after` 的公开事件。没传 `after` 时用请求头 `Last-Event-ID`，两个都没有就从 0 开始。

公开事件推给聊天页：`run.created`、`run.started`、`step.started`、`step.completed`、`model.started`、`model.completed`、`tool.started`、`tool.completed`、`tool.failed`、`tool.manual_review`、`run.waiting_for_user`、`run.requeued_by_sweeper`、`run.completed`、`run.failed`、`run.cancelled`。

内部事件留在库里，从 `/trace` 读，不进聊天时间线：`run.retry_scheduled`、`run.resumed`、`run.cancel_requested`、`run.enqueued`、`run.queue_stale`、`run.waiting_reminder`、`run.budget_warning`。跳过还是确认，读库里的 `run.resumed`。

库里的 payload 是完整的。公开接口去掉 `purpose`、`prompt` 和 token。页面文案在写入时放进 `display`。

`GET /runs/{id}/events/stream` 先把已落库的行推完，再 `LISTEN run_events`。通知到达，或 15 秒心跳到达，就按当前 `sequence` 再查一次。没有新行就发 `: heartbeat`，不占序号。一条 SSE 是 `id`（sequence）、`event`（event_type）、`data`（公开 payload）。`run.completed`、`run.failed`、`run.cancelled` 推出后关闭。`run.waiting_for_user` 不关闭。这条流不调模型、不调工具、不改状态。

正在生成的正文在 `output_json.text`。写正文不产生事件，页面边听事件流边读这条 run。

| | 取消 | 断连 |
|---|---|---|
| 谁发起 | 用户点停止 | 关页、切聊天、流被读完 |
| 后端 | `request_cancel`，worker 在循环里收手 | run 继续 |
| 前端 | `cancelRun` | abort 只停本地这条流 |

## 同一个会话

一个会话同时只跑一条 chat run。检查和领走锁在同一条会话行上（`claim_chat_turn`）。已经有一条 `running` 时，下一条留在 `queued`。Celery 碰到 `SessionBusy` 过两秒再试，名额可以去跑别的会话。确认和生成同时写会话时用 Redis 会话锁 `hold_session`。

## 工具

模型选中的工具都从 `execute_tool` 进去。`_finish_tool` 只做循环里的事：对上 context、处理跳过、把结果放回 context。给模型的 schema 只有名字、说明和 `parameters`。`side_effect`、`idempotency_required`、`timeout_seconds`、`retryable_errors`、`display` 由执行入口读。

调用顺序：

1. 按名字找工具。没有则 `TOOL_NOT_FOUND`，不调用函数。
2. 发信或 MCP 的开关关着：不占座，run 进入 `waiting_for_user`，事件里写 `tool_paused`。
3. 用 `parameters` 校验参数。只查 `required`、属性的 `type` 和 `enum`、嵌套对象、数组元素，以及 `additionalProperties: false`。不合法则 `TOOL_VALIDATION_ERROR`，不占座。失败行的键是 `{tool_name}:{tool_use_id}`，避免占住以后的业务键。
4. 发信和要幂等的 MCP 核对用户属于这条 run 的经销商。
5. 有 `max_tool_calls` 且已落库的调用达到上限：不占座，`tool.failed`，错误码 `COST_BUDGET_EXCEEDED`，整条 run 失败。空值表示不限制。
6. 副作用按参数算出业务键，查 `tool_calls`。已有行就不再执行。
7. `claim_tool_call`，状态 `running`，并写 `tool.started`。函数在事务外面跑。
8. 到 `timeout_seconds` 就不再采用返回值。守护线程停不掉，超时之后它若还在跑，写回不会改成成功。
9. `complete_tool_call` 在同一事务里写工具行、这一步的结束、checkpoint、`tool.completed` 或 `tool.failed`。副作用再写一行 `audit_logs`，`action` 是工具名，`target_type` 是 `tool_call`，不记参数和正文。

读工具的幂等键是 `{tool_name}:{tool_use_id}`，同一次提问里重复点名不会再执行，新的一次提问会再查。

副作用的键不含 `run_id` 和 `tool_use_id`，所以换一条 run 会撞上原来的行：不插入第二行，把已有结果交回，事件上标 `skipped_duplicate`。原来那行如果还是 `running`，改成 `response_lost`，不再执行。

| 工具 | 键 |
|---|---|
| `send_email` | `send_email:{org_id}:{to}:{sha256(subject + 换行 + body) 的前 16 位}`，抄送不进键 |
| MCP | `mcp:{org_id}:{tool_name}:{规范化参数的 sha256 前 16 位}`，参数按键名排序 |

`skip` 不进 `execute_tool`，记 `{skipped: true}`，也不发信。

| 工具 | 副作用 | 超时 | 可重试的错误 | 要确认 |
|---|---|---|---|---|
| `send_email` | 是 | 30 秒 | 无。超时记 `response_lost` | 是 |
| MCP | 是 | 60 秒 | 无。超时记 `response_lost` | 是 |
| `web_search`、`translate`、`exchange_rate` | 否 | 30 秒 | `TOOL_TIMEOUT`、`TOOL_RATE_LIMITED`、`TOOL_UPSTREAM_ERROR` | 否 |
| `data.query`、`brand.search` | 否 | 120 秒 | `TOOL_TIMEOUT`、`TOOL_UPSTREAM_ERROR` | 否 |
| `po.draft`、`contract.read`、`report.draft` | 否 | 300 秒 | 无。内部已有自己的模型重试 | 否 |

`po.draft` 改的是会话里的草稿，再调一次是在改这份草稿。真正建单是页面上的 `confirm_order`。`contract.read` 在浮窗确认之前不写合同。`report.draft` 把草稿交回对话。这三件标成副作用的话，第二次起草会被当成重复请求挡掉。

失败的 `error_json` 是 `{code, message, retryable}`。处理函数抛错或没返回对象时，code 是 `TOOL_UPSTREAM_ERROR`。`po.draft`、`contract.read`、`data.query`、`report.draft` 里「缺文件、有缺项、查询报错」是交给模型看的正常结果，记 `succeeded`。

| code | 何时 | 重试 |
|---|---|---|
| `TOOL_VALIDATION_ERROR` | 参数不符合 schema，或邮件地址、主题、正文不合法 | 否 |
| `TOOL_NOT_FOUND` | 这次会话的注册表里没有这个名字 | 否 |
| `TOOL_PERMISSION_DENIED` | 用户不属于这条 run 的经销商 | 否 |
| `TOOL_IDEMPOTENCY_REQUIRED` | 副作用工具没有算出键 | 否 |
| `TOOL_TIMEOUT` | 超过 `timeout_seconds` | 仅当该工具把它列进 `retryable_errors` |
| `TOOL_RATE_LIMITED` | 搜索、翻译、汇率等上游 429 | 同上 |
| `TOOL_UPSTREAM_ERROR` | 上游 5xx 或网络错误 | 同上 |

读工具的可重试错误在同一行上再调，最多 2 次（一共 3 次），不重新请模型，也不占用 worker 的执行次数。副作用的 `retryable_errors` 为空，一次都不自动重试。

工具错误交回循环之后，run 继续。worker 的重试只留给数据库、租约这类还没形成工具结果的失败。把工具错误当成整段重试，会把模型再跑一遍，副作用可能再执行一次。

连接在发出之前被拒绝（`ConnectionRefusedError`、`ConnectionAbortedError`、`SMTPConnectError`，以及对应的 errno）时，`executed` 为 false，错误码仍是 `TOOL_UPSTREAM_ERROR`。读工具可以在同一行上再试。已经交给 SMTP 之后断线，记在业务键那一行上，下一次相同的信不再发。`SMTPServerDisconnected` 按已经发出处理。

没配 SMTP 在占座之前拒绝，code 是 `TOOL_UPSTREAM_ERROR`。

`memory.fold` 和 `document.ocr` 不是模型选的工具，由 `backend/memory.py` 和 `backend/documents.py` 自己记轨迹。折叠大约按 2000 token 预算由代码触发。PDF 的 OCR 只在 `contract.read` 读到 PDF、且 run 仍是 `running` 时记成工具调用。

需要确认的工具在执行前把 run 停在 `waiting_for_user`。`resume` 把出站任务发出去并叫醒 worker。最近一次 `decision` 为 `skip` 时不调用；没有 `decision` 或为 `confirm` 时照常执行。

## 模型

对话调用从 `backend/llm/calls.py` 的 `call_model` 进去。`client` 由调用方传入。一次调用一行 `model_calls`、一行 `usage_records`，挂上这次的 `run_id` 和 `step_id`。厂商给了 usage 时 `usage_source` 为 `actual`。两边都是 0，或根本没有 usage，按本地估算（一个汉字大约一个 token，其余大约四个字符一个），`usage_source` 为 `estimated`。估算不记成厂商数字。

`max_model_calls` 或 `max_tokens` 有值且已经用满时，不打模型，写 `model.failed`，错误码 `COST_BUDGET_EXCEEDED`。这一步不把整条 run 改成失败。模型 429 或单次调用失败同样留下这一次的失败记录，run 继续。

org 日预算看当天 `usage_records` 的 token 和费用。`KA_ACCOUNT_ORG_DAILY_TOKENS` 和 `KA_ACCOUNT_ORG_DAILY_COST_CENTS` 默认 0，表示不封顶。接近 80% 时一天记一条内部事件 `run.budget_warning`。超过则不再打模型。

聊天循环里的 decide / answer 由 `run_loop` 记 `model.started`，由 `insert_model_call` 记 `model.completed`。工具内部的抽取、售后、报表、查数、代码修、折叠和会话标题把 `run_id` / `step_id` 传进 `call_model`。查数返回的行只做 JSON 转换，不截断行数。

每一轮直接进工具循环，系统提示是 `REACT`，工具名单走 `tools=`。

## 限流、并发、开关

限流是 Redis 固定窗口，key 带 org、用户、接口和分钟。默认每分钟每家 1000 次创建、每个用户 200 次创建、每家 10000 次读取、每个用户 2000 次读取。超过返回 429 `RATE_LIMITED`。`0` 表示这一档不限制。

并发也在 Redis，占满就拒绝并立刻还回计数：

| 闸门 | 起点 |
|---|---|
| 全局同时 running | 100 |
| 每个 org | 10 |
| 每个用户 | 3 |
| 每家发信和 MCP | 3 |
| 每家搜索、查数、翻译、汇率 | 20 |
| 每个模型 provider | 32 |

闸门在建 run、领 run、真正调用工具之前、`call_model` 打模型之前。模型闸门满时返回 `RATE_LIMITED`，不打厂商。

`SEND_EMAIL_ENABLED`、`MCP_TOOLS_ENABLED`、`QUEUE_ENABLED` 默认开。关上队列时 worker 不再领新的 `execute_run`，Celery 任务 30 秒后再试，已经领走的跑完。

`max_duration_seconds` 在领到 run 时看，清扫也会看。超过记 `failed`，错误码 `COST_BUDGET_EXCEEDED`。已经在 `waiting_for_user` 的保持等待。数据库语句超时默认 120 秒（`KA_ACCOUNT_STATEMENT_TIMEOUT_MS`），`0` 表示关掉。

失败日志带 `request_id`、`trace_id`、`run_id`、`org_id`、`user_id`、`outbox_id`、`step_id`、`tool_call_id`、`tool_name`、`error_code`、`executed`、`retryable`、`latency_ms`。密钥、token 和完整参数不进日志。`trace_id` 在建 run 时生成，穿过 outbox、Celery、模型和工具。

## worker 中途消失

业务重试写在 outbox 的 `next_attempt_at` 上。执行次数上限是 3，发布重试上限是 10。基础设施重试和租约清扫不占执行次数。Celery 打开 `acks_late`，worker 进程丢失时拒绝消息，任务可以再被领取。

同一条 `execute_run` 可能被投两次。五层挡住重复执行：

1. run 已是 `succeeded`、`failed` 或 `cancelled`：worker 直接返回。
2. 租约还没过期：另一个 worker 不领。慢的 `report.draft`、`po.draft` 不算卡住。
3. checkpoint 的 `last` 已经是 `tool_result:`：下一轮是模型，context 里的工具不再调用。
4. `last` 还是 `assistant:tool_use:`：不再请模型。按幂等键查 `tool_calls`。没有行才调用。已成功的行把结果写进 context。还在 `running` 的不调用；副作用改成 `response_lost` 并写 `tool.manual_review`。
5. `send_email` 和 MCP 的业务键撞上已有行：不再发出。

没有 checkpoint 时，才从这条 run 的用户消息开始。

清扫是 Compose 服务 `sweeper`，同一镜像，命令 `python -m backend.services.sweeper`，默认 15 秒一轮。它不跑循环，不调模型，不执行工具。

| 扫到 | 做什么 |
|---|---|
| `running`，租约过期，并且 `cancel_requested_at` 已有 | `cancelled` |
| `running`，租约过期，`attempts` 达到 3 | `failed` |
| `running`，租约过期，次数还没到 | 改回 `queued`，新写一行 outbox，公开事件 `run.requeued_by_sweeper` |
| `queued` 超过 10 分钟，outbox 已送出 | 只记内部事件 `run.queue_stale` |
| `waiting_for_user` 超过 1 天 | 只记内部事件 `run.waiting_reminder`，不自动确认 |
| `running` 且超过 `max_duration_seconds` | `failed`；若已请求取消则 `cancelled` |
| 副作用 `tool_calls` 停在 `running` 超过 90 秒 | `response_lost`，并写 `tool.manual_review` |
| outbox 仍是 `pending` | `dispatch_pending` |
| 已送出但 run 还在 `queued` 超过 30 秒 | 再投一次 |

## 回放、重试、重跑、修复

| 动作 | 做什么 | 会不会再次发信或改草稿 |
|---|---|---|
| 回放 | 按 `sequence` 读 `run_events` | 不会。不调模型，不调工具 |
| 重试 | 从 checkpoint 继续，或再领一次队列里的任务 | 可能再调模型。已完成的工具用已有结果。发信和 MCP 靠幂等键 |
| 重跑 | 新建一条 run，`parent_run_id` 指向旧的 | 会。这是一次新的执行。旧 run 停在 `failed` |
| 修复 | `POST /runs/{id}/repair` | 不改状态。只追加一条 `audit_logs`，`action` 是 `data.repair`，记下原因、操作者、改前和改后 |

## 部署

API、worker、sweeper 是三个 Compose 服务、同一份镜像。API 跑短请求，worker 跑模型和工具，sweeper 只清扫和补投递。一台机器上要多给 worker 吞吐，加大 `--concurrency`，或再起一个同样命令的 worker。看排队时长、待领消息、工具耗时和模型 429，不看 CPU。API 要扩的时候看延迟和 SSE 连接数。

迁移用 Alembic。发布时先升级再起新代码。加列用可空或带默认值。同一版里不删正在被旧代码读的列。旧事件少了字段时，读取方按没有这个字段处理。API 和 worker 必须对着同一版 schema。

## 监控

桌面 `/monitor` 按当前用户的经销商列出 run，可按状态和用户过滤。打开一条能看 step、模型调用、工具调用、租约、outbox 和死信。瀑布用 run 的创建和开始、outbox 的投递时间、`model_calls`、`tool_calls` 和 checkpoint，没有另一张 span 表。

`GET /metrics` 是同一份数的 Prometheus 文本。计数来自这几张表和带 `skipped_duplicate` 的事件，排队长度是 Redis 队列 `ka_account` 里还没领走的消息，在线订阅数是打开的事件流。卡住指排队超过 10 分钟，或 `running` 且租约已过期。

发消息那一跳的耗时，以及 `run.created` 的成功率，发生在写入之前，没有分母，页面上标成未采集。三个开关只显示环境变量的当前值。

告警用这些阈值：普通 run 完成时间 P95 < 60 秒，`run.failed` 比例 < 1%，发信和 MCP 错误率 < 0.1%，排队等待 P95 < 10 秒，卡住的 run < 10，单 org 每日费用不超过该 org 的预算。点开之后是五条处理步骤：

- **排队变长。** 看还没取走的 `execute_run`、正在跑的 run、worker 日志。worker 不够就加大并发。下游已经超时或 429 时，先降该工具或该模型的并发。
- **卡住的 run 变多。** 看 `lease_expires_at`、最后一条事件、最后一份 checkpoint，然后跑 sweeper。租约还没过期的慢任务不要收掉。
- **发信或 MCP 报错变多。** 看 `tool_calls.error_json`。确认下游异常后关上对应开关，后续调用进入 `waiting_for_user`，不自动重发。
- **模型 429 变多。** 降低该 provider 的并发，晚一点再试。没有第二家已经接好的模型，就不切换。
- **迁移失败。** 停止继续发布。看 Alembic 是否写到一半，用回滚把 schema 退回去。

## 还没做

### 治理字段

工具定义上可以再加 `tool_version`、`risk_level`、`approval_policy`、`network_policy`、`data_classification`、`audit_required`。现在没有代码读它们。审批继续用 `requires_approval`，审计继续由 `side_effect` 决定。等某条规则要按风险、网络或数据级别分支时，字段和读取它的代码一起加。

### 副作用对账

`response_lost` 表示请求可能已经发出，结果没有回来。下一步本应拿幂等键去问下游，再把这一行写成 `succeeded` 或 `failed`，并记下修复事件和 `audit_logs`。发信和现有 MCP 没有这个查询。在有可查询的下游之前，这一行保持 `response_lost`，只写 `tool.manual_review`，不自动重发。
