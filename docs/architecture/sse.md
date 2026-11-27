# SSE：进度和正文怎么推到页面

聊天页不把模型流直接接到浏览器。Worker 把事实写进 Postgres，API 只读 `run_events`，用 SSE 按 `sequence` 推给页面。断线、漏通知、worker 重试都不改这条顺序。租约和状态机见 [runtime.md](runtime.md)，一轮聊天的时间顺序见 [../performance/chat-flow.md](../performance/chat-flow.md)，事件语义和 `run.created` 不保证已执行见 [../performance/run.md](../performance/run.md)。

```text
worker 同一事务
  agent_runs / run_events（sequence + 1）
  pg_notify('run_events', run_id)     ← 失败只丢掉这一次通知

GET /api/runs/{id}/events/stream?after={sequence}
  先读 sequence > after 的公开事件
  LISTEN run_events
  通知命中，或 15 秒到点，再按 sequence 读一次
  编成 SSE 帧推出去

聊天页 followRun
  活动条 ← 公开事件的 display
  正在生成的正文 ← run.reply_updated，并用 GET /runs 的 output_json 补一刀
```

这条流不调模型、不调工具、不改 `run.status`。关掉页面或断开 SSE 不会取消 run；停止按钮才走 `POST /runs/{id}/cancel`。

## 1. 事件先落库

写入在 `backend/db/database/run.py` 的 `_write_event` / `_append_event`。一次追加做四件事，和状态变更在同一个事务里：

1. `SELECT … FOR UPDATE` 锁住这条 `agent_runs` 行。同一事务里 `_transition` 已经拿过这把锁时，第二次是同一把。
2. `max(sequence) + 1`。`run_events` 有 `UNIQUE (run_id, sequence)`，序号只对这一条 run 递增。
3. 没有可用的 `display` 时，用 `event_display` 补一句页面文案再写入。调用方已经写了 `display` 的，以调用方为准。
4. 同一事务里 `pg_notify('run_events', run_id)`。通道名是 `RUN_EVENT_CHANNEL`。载荷只有 `run_id`，没有事件正文。

`pg_notify` 包在 savepoint 里。通知失败时子事务回滚，外面的提交照常：状态和 `run_events` 都在。漏掉的通知靠第 3 节的 15 秒回表补上。

库里的 payload 是完整的。`purpose`、`prompt`、token、`reasoning` 都留在行上，给 `/trace` 用。公开接口读的时候再丢掉，见第 4 节。

## 2. 谁看得到

聊天页只订**父 chat run**。子 run 的 step / model 事件在 `_append_event` 里沿 `owner_run_id` 抄到每一层 owner，payload 带 `projected_from` 和 `child_run_id`。抄过一次的行（已有 `projected_from`）不再向上抄。`run_type = open` 的标题精修不抄，OPEN 事件不进聊天 SSE。

会抄上去的只有 `PROJECTED_CHILD_EVENTS`：`step.*`、`model.*`（含 route / quality gate）。工具事件不抄。父 run 上的 `tool.started` 是 `commit_task_wave` 在派波时直接写在父 run 上的，每个子任务一条。

公开集合是 `PUBLIC_RUN_EVENTS`（`backend/services/run_names.py`）。`/events` 和 `/events/stream` 都先按这个集合过滤：

| 进聊天时间线 | 只留在库里，`/trace` 能看见 |
|---|---|
| `run.created` `run.started` | `run.enqueued` `run.retry_scheduled` |
| `step.started` `step.completed` | `run.resumed` `run.cancel_requested` |
| `model.started` `model.completed` | `run.queue_stale` `run.waiting_reminder` |
| `tool.started` `tool.completed` `tool.failed` `tool.manual_review` | `run.budget_warning` |
| `run.reply_updated` | `step.failed` `model.failed` 以及 route / quality 事件 |
| `run.waiting_for_user` `run.waiting_for_tasks` `run.requeued_by_sweeper` | |
| `run.completed` `run.failed` `run.cancelled` | |

`step.failed`、`model.failed` 会抄到父 run 的 `run_events`，但公开列表不包含它们，所以聊天时间线看不到。页面文案只看 payload 里的 `display`。`run.created` 没有 `display`，步骤条不画「已入队」。

## 3. 流本身

入口是 `GET /api/runs/{id}/events/stream`（`backend/routes/runs.py` → `backend/services/run_stream.py`）。

鉴权只认 `X-User-Id`。`org_id` 取这个用户的 `distributor_id`，不认请求头或 query 里的 org。这条 run 不属于该经销商时，在写出第一个字节之前返回 404：路由先调一次 `list_events`，生成器里再读同一条查询。

游标：query `after` 优先，否则用请求头 `Last-Event-ID`，都没有则从 0 开始。非法或负数是 422。读的是 `sequence > after` 的公开行。桌面不用浏览器 `EventSource`（它不能带 `X-User-Id`），用 `fetch` 自己读 body，所以续传走 `?after=`，不靠 `Last-Event-ID`。

一帧：

```text
id: {sequence}
event: {event_type}
data: {公开 payload 的 JSON}

```

`data` 是去掉私有字段之后的 `payload_json`，不是整行。心跳是 SSE 注释，不占序号，解析时丢掉：

```text
: heartbeat
```

生成器的顺序：

1. 把当前游标之后已经落库的公开事件全部推出。其中若有 `run.completed`、`run.failed`、`run.cancelled`，推完就关流。
2. 另开一条 autocommit 的 psycopg 连接（不走 SQLAlchemy 会话），`LISTEN run_events`。
3. 阻塞等通知，最长 `HEARTBEAT_SECONDS`（15 秒）。通知的 payload 必须等于这条 `run_id`；别的 run 的通知被丢掉，继续等到超时。
4. 不论是被通知叫醒还是超时，都再按游标查一次表。有新行就推；超时且没有新行才发心跳。

`LISTEN` 之前提交的通知会丢。第一次查询已经把当时的行推完；夹在「查完」和「LISTEN」之间的行，以及 `pg_notify` 失败的行，都由下一次叫醒或 15 秒心跳按 `sequence` 补上。权威顺序在表上，不在通知里。

`run.waiting_for_user` 不关流。服务端只在三个终态事件推出后关闭。连接数记在 Redis（`note_sse`），Redis 不可用时计数跳过，流照常推。响应头是 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`，避免反向代理把帧攒成一块再吐。

## 4. 公开读的时候丢掉什么

`list_events`（`backend/services/run_service.py`）对每一行去掉 `PRIVATE_PAYLOAD_KEYS`：`purpose`、`prompt`、`token`、`tokens`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`reasoning`。

模型的 `reasoning_content` 写在 `model_calls.response_json.reasoning`，也会进 `run.reply_updated` 的库存 payload。公开帧里没有它。`/trace` 返回库存原行，不过这道过滤。

## 5. 正在生成的正文

正文不走模型到浏览器的直连。ANSWER（`backend/orchestrator/parent.py` 的 `on_text`）每收到一段增长中的文本，就调 `note_running_reply`：

- 仅当 run 仍是 `running` 时写入，状态不变。
- `agent_runs.output_json` 写成 `{text, generation_id, reasoning?}`。
- `text` 非空时再追加一条 `run.reply_updated`，于是触发 `NOTIFY`。空字符串只改 `output_json`，不追加事件。

`generation_id` 标记这一次生成。worker 放弃一段失败的流时（`_abandon_running_reply`）换成新的 id、把 `text` 写成空。页面见到新 id 就丢掉上一段 token。

所以一次可见的增量是：worker 提交 `output_json` + `run.reply_updated` → `NOTIFY` → SSE 推出 `event: run.reply_updated`，`data` 里是当前累计全文，不是一个 delta。

## 6. 页面怎么消费

`frontend/src/api.ts` 的 `followRun`：

1. `GET /runs/{id}` 拿状态和 `output_json`，再 `GET /runs/{id}/events` 拿已有公开事件。
2. 状态已是 `succeeded`、`failed`、`cancelled`、`waiting_for_user` 就不再订流。`waiting_for_tasks` 不算结束，父 run 还要被唤醒。
3. 否则 `fetch` 流，按空行切帧，`parseSse` 读 `id` / `event` / `data`。以 `:` 开头的心跳行丢掉。序号去重后按 `sequence` 排序。
4. 流结束或出错后等 400ms，用当前最大序号当 `after` 再连。服务端关掉终态流之后，这一侧也会再连，直到第 5 步的轮询看见终态。
5. 与流并行，大约每 800ms `GET /runs/{id}`。轮询看见上面四个状态就停，并再用 `after` 把尾部事件拉齐。终态以这次轮询为准，不单靠 SSE 被对端关掉。

`Chat.tsx` 每次更新都做两件事：

- `activitiesFromEvents`：有 `display` 的公开事件画成步骤。排队名次来自 `GET /runs` 的 `queue_ahead`，不是事件。
- 正文先用 `liveFromRun`（`output_json`），再用 `liveFromEvents`（最新一条 `run.reply_updated`）。后一次覆盖前一次，所以 SSE 先到就先画字，轮询是同一份全文的备份。新的 `generation_id` 清掉旧字。

run 结束后，助手消息改从 `chat_sessions.history` 读。流式阶段看到的字还在 `output_json` 里，history 要等 `save_reply`。

## 7. 和另外两条通道不要混

| | SSE | `GET /runs/{id}` | `GET /runs/{id}/trace` |
|---|---|---|---|
| 读什么 | 公开 `run_events` | 当前状态、`output_json`、排队名次 | steps、全部事件（含内部、含私有字段） |
| 推还是拉 | 长连接 | 约 800ms 轮询 | 终态或排障时拉一次 |
| 漏了怎么办 | `after` 续传；15 秒回表 | 下一次轮询 | 再请求 |

Redis 不参与这条流。Celery broker、波次回合箱、会话锁都不是 SSE 的通道。
