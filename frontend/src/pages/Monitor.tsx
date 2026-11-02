import { useEffect, useState } from "react";

import { api } from "../api";
import { useDesk } from "../desk";
import { Badge, Metrics, PageHead } from "../ui";

type Board = {
  switches: { send_email: boolean; mcp: boolean; queue: boolean };
  slo: { name: string; target: string; value: string; ok: boolean | null }[];
  alerts: { signal: string; action: string; runbook: string | null }[];
  runbooks: { id: string; title: string; body: string }[];
  metrics: {
    created: number;
    succeeded: number;
    failed: number;
    duration_p95: number | null;
    queue_lag_p95: number | null;
    pending_jobs: number;
    active_runs: number;
    sse_connections: number;
    stuck_runs: number;
    cost_cents: number;
    duplicates: number;
  };
};

type RunRow = {
  id: string;
  user_id: string;
  run_type: string;
  status: string;
  trace_id: string | null;
  created_at: string;
  lease_owner: string | null;
  lease_expires_at: string | null;
};

type Trace = {
  run: RunRow & { started_at?: string | null; ended_at?: string | null; attempts?: number };
  steps: { id: string; step_index: number; name: string; status: string }[];
  events: { sequence: number; event_type: string; payload_json?: { display?: string } }[];
  model_calls: { id: string; model: string; status: string; latency_ms: number | null; created_at: string }[];
  tool_calls: {
    id: string;
    tool_name: string;
    status: string;
    latency_ms: number | null;
    started_at: string | null;
    created_at: string;
  }[];
  outbox: { id: string; job_type: string; status: string; attempts: number; created_at: string; dispatch_at: string }[];
  dead_letters: { id: string; job_type: string; attempts: number; error_json?: { message?: string } }[];
  checkpoints: { id: string; checkpoint_index: number; created_at: string }[];
};

type Span = { kind: "queue" | "dispatch" | "model" | "tool" | "checkpoint"; name: string; start: number; ms: number };

const STATUSES = ["", "queued", "running", "waiting_for_user", "succeeded", "failed", "cancelled"];

function headers(userId: string): HeadersInit {
  return { "X-User-Id": userId };
}

function seconds(value: number | null): string {
  if (value == null) return "—";
  return `${value.toFixed(2)}s`;
}

function waterfall(trace: Trace): Span[] {
  const origin = Date.parse(trace.run.created_at);
  if (!Number.isFinite(origin)) return [];
  const spans: Span[] = [];
  const started = trace.run.started_at ? Date.parse(trace.run.started_at) : NaN;
  if (Number.isFinite(started)) {
    spans.push({ kind: "queue", name: "领跑", start: 0, ms: Math.max(0, started - origin) });
  }
  for (const job of trace.outbox) {
    const at = Date.parse(job.created_at);
    const sent = Date.parse(job.dispatch_at);
    spans.push({
      kind: "dispatch",
      name: "投递",
      start: Math.max(0, at - origin),
      ms: Math.max(0, sent - at),
    });
  }
  for (const call of trace.model_calls) {
    const at = Date.parse(call.created_at);
    const ms = call.latency_ms ?? 0;
    spans.push({ kind: "model", name: call.model, start: Math.max(0, at - origin - ms), ms: Math.max(ms, 0) });
  }
  for (const call of trace.tool_calls) {
    const at = Date.parse(call.started_at || call.created_at);
    spans.push({
      kind: "tool",
      name: call.tool_name,
      start: Math.max(0, at - origin),
      ms: Math.max(call.latency_ms ?? 0, 0),
    });
  }
  for (const point of trace.checkpoints) {
    const at = Date.parse(point.created_at);
    spans.push({ kind: "checkpoint", name: `checkpoint ${point.checkpoint_index}`, start: Math.max(0, at - origin), ms: 0 });
  }
  spans.sort((left, right) => left.start - right.start);
  return spans;
}

export function MonitorPage() {
  const desk = useDesk();
  const userId = desk.user?.id ?? "";
  const [board, setBoard] = useState<Board | null>(null);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [status, setStatus] = useState("");
  const [scope, setScope] = useState<"user" | "org">("user");
  const [trace, setTrace] = useState<Trace | null>(null);
  const [runbook, setRunbook] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!userId) return;
    const query = new URLSearchParams();
    if (status) query.set("status", status);
    if (scope === "user") query.set("user_id", userId);
    const suffix = query.toString() ? `?${query}` : "";
    setError(null);
    Promise.all([
      api<Board>("/api/monitor", { headers: headers(userId) }),
      api<RunRow[]>(`/api/runs${suffix}`, { headers: headers(userId) }),
    ]).then(([nextBoard, nextRuns]) => {
      setBoard(nextBoard);
      setRuns(nextRuns);
    }).catch((err: Error) => setError(err.message));
  }, [userId, status, scope]);

  function openRun(id: string) {
    setError(null);
    api<Trace>(`/api/runs/${encodeURIComponent(id)}/trace`, { headers: headers(userId) })
      .then(setTrace)
      .catch((err: Error) => setError(err.message));
  }

  const openBook = board?.runbooks.find((item) => item.id === runbook) ?? null;
  const spans = trace ? waterfall(trace) : [];
  const spanTotal = spans.reduce((max, span) => Math.max(max, span.start + span.ms), 1);

  return (
    <>
      <PageHead
        title="监控台"
        caption="数字来自已经写入的 run、模型和工具记录。三个开关显示当前环境变量，改它们要改环境并重启。"
      />
      {error && <div className="error">{error}</div>}
      {!userId && <div className="empty">先在左侧选一个用户。</div>}
      {board && (
        <>
          <Metrics items={[
            { label: "已创建", value: String(board.metrics.created) },
            { label: "已完成", value: String(board.metrics.succeeded) },
            { label: "已失败", value: String(board.metrics.failed) },
            { label: "完成 P95", value: seconds(board.metrics.duration_p95) },
            { label: "排队 P95", value: seconds(board.metrics.queue_lag_p95) },
            { label: "待领任务", value: String(board.metrics.pending_jobs) },
            { label: "正在跑", value: String(board.metrics.active_runs) },
            { label: "卡住", value: String(board.metrics.stuck_runs) },
            { label: "在线订阅", value: String(board.metrics.sse_connections) },
            { label: "挡住的重复", value: String(board.metrics.duplicates) },
            { label: "费用（分）", value: String(board.metrics.cost_cents) },
          ]} />
          <div className="filters">
            <span className="badge info">发信 {board.switches.send_email ? "开" : "关"}</span>
            <span className="badge info">MCP {board.switches.mcp ? "开" : "关"}</span>
            <span className="badge info">领新任务 {board.switches.queue ? "开" : "关"}</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>目标</th><th>阈值</th><th>现在</th><th></th></tr>
              </thead>
              <tbody>
                {board.slo.map((row) => (
                  <tr key={row.name}>
                    <td>{row.name}</td>
                    <td>{row.target}</td>
                    <td>{row.value}</td>
                    <td>{row.ok == null ? "未采集" : row.ok ? "达标" : "超过"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <section className="panel">
            <h2>告警</h2>
            {board.alerts.length === 0 && <p className="muted">当前没有超过阈值的信号。</p>}
            {board.alerts.map((alert) => (
              <div className="warn" key={alert.signal}>
                <strong>{alert.signal}</strong>
                <div>{alert.action}</div>
                {alert.runbook && (
                  <button className="btn" type="button" onClick={() => setRunbook(alert.runbook)}>处理步骤</button>
                )}
              </div>
            ))}
            <div className="filters">
              {board.runbooks.map((item) => (
                <button
                  key={item.id}
                  className="btn"
                  type="button"
                  onClick={() => setRunbook(item.id)}
                >{item.title}</button>
              ))}
            </div>
            {openBook && (
              <div className="card">
                <h3>{openBook.title}</h3>
                <p>{openBook.body}</p>
              </div>
            )}
          </section>
        </>
      )}
      <section className="panel">
        <h2>Run</h2>
        <div className="filters">
          <label>状态
            <select aria-label="状态" value={status} onChange={(event) => setStatus(event.target.value)}>
              {STATUSES.map((item) => <option key={item || "all"} value={item}>{item || "全部"}</option>)}
            </select>
          </label>
          <label>范围
            <select aria-label="范围" value={scope} onChange={(event) => setScope(event.target.value as "user" | "org")}>
              <option value="user">这个用户</option>
              <option value="org">整个经销商</option>
            </select>
          </label>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>状态</th><th>类型</th><th>用户</th><th>trace</th><th>创建</th><th></th></tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id} className="monitor-run">
                  <td><Badge status={run.status} /></td>
                  <td>{run.run_type}</td>
                  <td>{run.user_id}</td>
                  <td className="mono">{run.trace_id || "—"}</td>
                  <td>{run.created_at}</td>
                  <td><button className="btn" type="button" onClick={() => openRun(run.id)}>打开</button></td>
                </tr>
              ))}
              {runs.length === 0 && <tr><td colSpan={6}>没有 run。</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
      {trace && (
        <section className="panel">
          <h2>这条 run</h2>
          <p className="muted">
            租约 {trace.run.lease_owner || "无"}
            {trace.run.lease_expires_at ? `，到 ${trace.run.lease_expires_at}` : ""}
            。重放只读事件，不重新调用模型和工具。
          </p>
          <h3>瀑布</h3>
          {spans.length === 0 && <p className="muted">还没有模型调用或工具调用。</p>}
          <div className="waterfall">
            {spans.map((span, index) => (
              <div className="waterfall-row" key={`${span.kind}-${span.name}-${index}`}>
                <span>{span.name}</span>
                <span className="waterfall-track">
                  <span
                    className={`waterfall-bar ${span.kind}`}
                    style={{
                      left: `${(span.start / spanTotal) * 100}%`,
                      width: `${Math.max((span.ms / spanTotal) * 100, 0.8)}%`,
                    }}
                  />
                </span>
                <span>{(span.ms / 1000).toFixed(2)}s</span>
              </div>
            ))}
          </div>
          <h3>Step</h3>
          <RecordTable rows={trace.steps.map((step) => `${step.step_index}. ${step.name} ${step.status}`)} />
          <h3>出站</h3>
          <RecordTable rows={trace.outbox.map((job) => `${job.job_type} ${job.status} 尝试 ${job.attempts}`)} />
          <h3>死信</h3>
          <RecordTable rows={trace.dead_letters.map((job) => `${job.job_type} ${job.error_json?.message || ""}`)} />
          <h3>重放</h3>
          <ol className="replay">
            {trace.events.map((event) => (
              <li key={event.sequence}>
                <span className="mono">{event.sequence}</span> {event.event_type}
                {event.payload_json?.display ? ` · ${event.payload_json.display}` : ""}
              </li>
            ))}
          </ol>
        </section>
      )}
    </>
  );
}

function RecordTable({ rows }: { rows: string[] }) {
  if (rows.length === 0) return <p className="muted">无</p>;
  return (
    <ul className="replay">
      {rows.map((row) => <li key={row}>{row}</li>)}
    </ul>
  );
}
