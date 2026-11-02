import { useEffect, useRef, useState, type ReactNode } from "react";
import * as echarts from "echarts";

import { api } from "../api";
import { useDesk } from "../desk";
import { Badge, Metrics, PageHead } from "../ui";

type Board = {
  switches: { send_email: boolean; mcp: boolean; queue: boolean };
  slo: { name: string; target: string; value: string; ok: boolean | null }[];
  alerts: { signal: string; action: string; runbook: string | null }[];
  runbooks: { id: string; title: string; body: string }[];
  charts: {
    hours: { at: string; created: number; failed: number }[];
    duration: { label: string; count: number; tail: boolean }[];
    duration_p95: number | null;
    queue: { label: string; count: number; tail: boolean }[];
    queue_p95: number | null;
    levels: { label: string; value: number; limit: number | null }[];
  };
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
        caption="看最近 24 小时的失败有没有爬升，完成时间和排队时间的尾巴，以及当前卡住和待领的水位。累计总数不在这里。"
      />
      {error && <div className="error">{error}</div>}
      {!userId && <div className="empty">先在左侧选一个用户。</div>}
      {board && (
        <>
          <Metrics items={headlines(board)} />
          <div className="monitor-grid">
            <ChartCard title="每小时新建与失败" note="看失败那条是不是在往上走，不看累计了多少。">
              <MonitorChart option={hourOption(board.charts.hours)} />
            </ChartCard>
            <ChartCard title="完成时间" note={`近 24 小时的分布，P95 ${seconds(board.charts.duration_p95)}。超过 60 秒的落在右边。`}>
              <MonitorChart option={barOption(board.charts.duration)} />
            </ChartCard>
            <ChartCard title="排队时长" note={`近 24 小时从创建到开始，P95 ${seconds(board.charts.queue_p95)}。超过 10 秒的落在右边。`}>
              <MonitorChart option={barOption(board.charts.queue)} />
            </ChartCard>
            <ChartCard title="当前水位" note="卡住和待领没有历史曲线，这里只是打开页面这一刻。虚线是卡住和正在跑的上限。">
              <MonitorChart option={levelOption(board.charts.levels)} />
            </ChartCard>
          </div>
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

const MOSS = "#2c6b4a";
const BAD = "#9d2c2c";
const INK = "#6f675e";
const LINE = "#e4dccf";

function headlines(board: Board): { label: string; value: string }[] {
  const created = board.charts.hours.reduce((sum, point) => sum + point.created, 0);
  const failed = board.charts.hours.reduce((sum, point) => sum + point.failed, 0);
  const stuck = board.charts.levels.find((item) => item.label === "卡住")?.value ?? 0;
  const pending = board.charts.levels.find((item) => item.label === "待领")?.value ?? 0;
  const ratio = created === 0 ? "—" : `${((failed / created) * 100).toFixed(1)}%`;
  return [
    { label: "24h 新建", value: String(created) },
    { label: "24h 失败", value: `${failed} · ${ratio}` },
    { label: "完成 P95", value: seconds(board.charts.duration_p95) },
    { label: "排队 P95", value: seconds(board.charts.queue_p95) },
    { label: "卡住", value: String(stuck) },
    { label: "待领", value: String(pending) },
  ];
}

function hourOption(hours: Board["charts"]["hours"]): echarts.EChartsOption {
  return {
    tooltip: { trigger: "axis" },
    legend: { top: 0, right: 8, data: ["新建", "失败"], textStyle: { color: INK, fontSize: 12 } },
    grid: { left: 36, right: 12, top: 36, bottom: 28 },
    xAxis: {
      type: "category",
      data: hours.map((point) => hourLabel(point.at)),
      axisLabel: { color: INK, fontSize: 10 },
      axisLine: { lineStyle: { color: LINE } },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLabel: { color: INK, fontSize: 10 },
      splitLine: { lineStyle: { color: LINE } },
    },
    series: [
      { name: "新建", type: "bar", data: hours.map((point) => point.created), itemStyle: { color: MOSS }, barMaxWidth: 10 },
      { name: "失败", type: "bar", data: hours.map((point) => point.failed), itemStyle: { color: BAD }, barMaxWidth: 10 },
    ],
  };
}

function barOption(bars: { label: string; count: number; tail: boolean }[]): echarts.EChartsOption {
  return {
    tooltip: { trigger: "axis" },
    grid: { left: 36, right: 12, top: 16, bottom: 28 },
    xAxis: {
      type: "category",
      data: bars.map((bar) => bar.label),
      axisLabel: { color: INK, fontSize: 10 },
      axisLine: { lineStyle: { color: LINE } },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLabel: { color: INK, fontSize: 10 },
      splitLine: { lineStyle: { color: LINE } },
    },
    series: [{
      type: "bar",
      data: bars.map((bar) => ({ value: bar.count, itemStyle: { color: bar.tail ? BAD : MOSS } })),
      barMaxWidth: 22,
    }],
  };
}

function levelOption(levels: Board["charts"]["levels"]): echarts.EChartsOption {
  const marks = levels.flatMap((level) => (
    level.limit == null ? [] : [{ xAxis: level.limit, label: { formatter: `${level.label} ${level.limit}`, color: INK, fontSize: 10 } }]
  ));
  return {
    tooltip: { trigger: "axis" },
    grid: { left: 56, right: 24, top: 16, bottom: 24 },
    xAxis: {
      type: "value",
      minInterval: 1,
      axisLabel: { color: INK, fontSize: 10 },
      splitLine: { lineStyle: { color: LINE } },
    },
    yAxis: {
      type: "category",
      data: levels.map((level) => level.label),
      axisLabel: { color: INK },
      axisLine: { lineStyle: { color: LINE } },
    },
    series: [{
      type: "bar",
      data: levels.map((level) => ({
        value: level.value,
        itemStyle: { color: level.limit != null && level.value >= level.limit ? BAD : MOSS },
      })),
      barMaxWidth: 18,
      markLine: marks.length === 0 ? undefined : {
        symbol: "none",
        lineStyle: { type: "dashed", color: "#8a5a12" },
        data: marks,
      },
    }],
  };
}

function hourLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

function ChartCard({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return (
    <section className="panel">
      <h2>{title}</h2>
      <p className="muted">{note}</p>
      {children}
    </section>
  );
}

function MonitorChart({ option }: { option: echarts.EChartsOption }) {
  const node = useRef<HTMLDivElement>(null);
  const signature = JSON.stringify(option);
  useEffect(() => {
    const el = node.current;
    if (!el) return;
    const view = echarts.init(el);
    view.setOption(option);
    const observer = new ResizeObserver(() => view.resize());
    observer.observe(el);
    return () => {
      observer.disconnect();
      view.dispose();
    };
  }, [signature, option]);
  return <div className="chart-echarts" ref={node} />;
}

function RecordTable({ rows }: { rows: string[] }) {
  if (rows.length === 0) return <p className="muted">无</p>;
  return (
    <ul className="replay">
      {rows.map((row) => <li key={row}>{row}</li>)}
    </ul>
  );
}
