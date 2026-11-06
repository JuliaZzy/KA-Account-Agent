import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api";
import { useDesk } from "../desk";
import { Badge, DataTable, PageHead, Tabs } from "../ui";

type RunRow = {
  id: string;
  user_id: string;
  run_type: string;
  status: string;
  trace_id: string | null;
  created_at: string;
};

type Trace = {
  run: Record<string, unknown>;
  steps: Record<string, unknown>[];
  events: Record<string, unknown>[];
  model_calls: Record<string, unknown>[];
  tool_calls: Record<string, unknown>[];
  checkpoints: Record<string, unknown>[];
  usage_records: Record<string, unknown>[];
  audit_logs: Record<string, unknown>[];
  outbox: Record<string, unknown>[];
  dead_letters: Record<string, unknown>[];
};

const TAB_META = [
  { label: "agent_runs", rows: (trace: Trace) => [trace.run] },
  { label: "agent_steps", rows: (trace: Trace) => trace.steps },
  { label: "run_events", rows: (trace: Trace) => trace.events },
  { label: "checkpoints", rows: (trace: Trace) => trace.checkpoints },
  { label: "model_calls", rows: (trace: Trace) => trace.model_calls },
  { label: "tool_calls", rows: (trace: Trace) => trace.tool_calls },
  { label: "usage_records", rows: (trace: Trace) => trace.usage_records ?? [] },
  { label: "audit_logs", rows: (trace: Trace) => trace.audit_logs ?? [] },
  { label: "outbox_jobs", rows: (trace: Trace) => trace.outbox },
  { label: "dead_letter_jobs", rows: (trace: Trace) => trace.dead_letters },
] as const;

const COLUMN_ORDER = [
  "id",
  "run_id",
  "sequence",
  "step_index",
  "checkpoint_index",
  "status",
  "event_type",
  "step_type",
  "name",
  "tool_name",
  "model",
  "job_type",
  "created_at",
];

function headers(userId: string): HeadersInit {
  return { "X-User-Id": userId };
}

function sortKeys(keys: string[]): string[] {
  const rank = new Map(COLUMN_ORDER.map((key, index) => [key, index]));
  return [...keys].sort((left, right) => {
    const leftRank = rank.get(left) ?? 100;
    const rightRank = rank.get(right) ?? 100;
    if (leftRank !== rightRank) return leftRank - rightRank;
    return left.localeCompare(right);
  });
}

function isJsonColumn(key: string): boolean {
  return key.endsWith("_json") || key === "state_json";
}

function cellValue(value: unknown): ReactNode {
  if (value == null || value === "") return "—";
  if (typeof value === "object") {
    return <pre className="json-cell">{JSON.stringify(value, null, 2)}</pre>;
  }
  return String(value);
}

function tableColumns(rows: Record<string, unknown>[]) {
  const keys = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) keys.add(key);
  }
  return sortKeys([...keys]).map((key) => ({
    key,
    label: key,
    wrap: isJsonColumn(key),
    render: (row: Record<string, unknown>) => cellValue(row[key]),
  }));
}

export function RunDataPage() {
  const desk = useDesk();
  const userId = desk.user?.id ?? "";
  const [search, setSearch] = useSearchParams();
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [tab, setTab] = useState<string>(TAB_META[0].label);
  const [error, setError] = useState<string | null>(null);
  const runId = search.get("run_id") ?? "";

  useEffect(() => {
    if (!userId) return;
    setError(null);
    api<RunRow[]>("/api/runs?limit=100", { headers: headers(userId) })
      .then(setRuns)
      .catch((err: Error) => setError(err.message));
  }, [userId]);

  useEffect(() => {
    if (!userId || !runId) {
      setTrace(null);
      return;
    }
    setError(null);
    api<Trace>(`/api/runs/${encodeURIComponent(runId)}/trace`, { headers: headers(userId) })
      .then(setTrace)
      .catch((err: Error) => setError(err.message));
  }, [userId, runId]);

  const activeTab = TAB_META.find((item) => item.label === tab) ?? TAB_META[0];
  const rows = useMemo(() => (trace ? activeTab.rows(trace) : []), [trace, activeTab]);
  const columns = useMemo(() => tableColumns(rows), [rows]);

  function chooseRun(id: string) {
    const next = new URLSearchParams(search);
    if (id) next.set("run_id", id);
    else next.delete("run_id");
    setSearch(next, { replace: true });
  }

  return (
    <>
      <PageHead
        title="Run 表数据"
        caption="按 run 查看 agent_runs、steps、events、model/tool calls、checkpoints、usage、audit、outbox 和 dead letter 的原始行。"
      />
      {error && <div className="error">{error}</div>}
      {!userId && <div className="empty">先在左侧选一个用户。</div>}
      {userId && (
        <>
          <div className="filters">
            <label>
              Run
              <select
                aria-label="Run"
                value={runId}
                onChange={(event) => chooseRun(event.target.value)}
              >
                <option value="">选一条 run…</option>
                {runs.map((run) => (
                  <option key={run.id} value={run.id}>
                    {run.id.slice(0, 8)}… · {run.run_type} · {run.status} · {run.created_at}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {runId && !trace && !error && <p className="muted">加载中…</p>}
          {trace && (
            <>
              <p className="muted">
                <Badge status={String(trace.run.status)} /> {String(trace.run.run_type)} · 用户 {String(trace.run.user_id)} · trace{" "}
                <span className="mono">{String(trace.run.trace_id || "—")}</span>
              </p>
              <Tabs labels={[...TAB_META.map((item) => item.label)]} value={tab} onChange={setTab} />
              <p className="muted">{rows.length} 行</p>
              {rows.length === 0 && <p className="empty">这张表里没有行。</p>}
              {rows.length > 0 && <DataTable columns={columns} rows={rows} />}
            </>
          )}
        </>
      )}
    </>
  );
}
