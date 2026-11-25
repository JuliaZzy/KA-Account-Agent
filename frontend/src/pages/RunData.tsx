import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api";
import { useDesk } from "../desk";
import { clip } from "../format";
import { Badge, DataTable, PageHead, Tabs } from "../ui";

type RunRow = {
  id: string;
  org_id: string;
  user_id: string;
  run_type: string;
  status: string;
  owner_run_id: string | null;
  created_at: string;
  ended_at: string | null;
  input_json: Record<string, unknown> | null;
};

type ListedRun = {
  id: string;
  status: string;
  runType: string;
  sessionId: string;
  sessionName: string;
  taskId: string;
  taskName: string;
  orgName: string;
  userName: string;
  createdAt: string;
  createdLabel: string;
  endedLabel: string;
  ownerRunId: string;
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

function textOf(raw: unknown): string {
  return typeof raw === "string" ? raw.trim() : "";
}

function nestedInput(input: Record<string, unknown>): Record<string, unknown> | null {
  const nested = input.input;
  if (!nested || typeof nested !== "object" || Array.isArray(nested)) return null;
  return nested as Record<string, unknown>;
}

function sessionName(input: Record<string, unknown>, titles: Map<string, string>): string {
  const titled = titles.get(textOf(input.session_id));
  if (titled) return titled;
  return textOf(input.provisional_title) || textOf(input.message);
}

function taskName(input: Record<string, unknown>): string {
  const nested = nestedInput(input);
  if (nested) {
    const question = textOf(nested.question) || textOf(nested.query);
    if (question) return question;
    const product = textOf(nested.product_name);
    if (product) return product;
  }
  return textOf(input.name) || textOf(input.tool_name);
}

function stamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function listRun(
  run: RunRow,
  titles: Map<string, string>,
  orgName: string,
  userName: string,
): ListedRun {
  const input = run.input_json ?? {};
  return {
    id: run.id,
    status: run.status,
    runType: run.run_type,
    sessionId: textOf(input.session_id),
    sessionName: sessionName(input, titles),
    taskId: textOf(input.task_id),
    taskName: taskName(input),
    orgName: orgName || run.org_id,
    userName: userName || run.user_id,
    createdAt: run.created_at,
    createdLabel: stamp(run.created_at),
    endedLabel: stamp(run.ended_at),
    ownerRunId: run.owner_run_id ?? "",
  };
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

function matches(row: ListedRun, query: string): boolean {
  if (!query) return true;
  const haystack = [
    row.id,
    row.status,
    row.runType,
    row.sessionId,
    row.sessionName,
    row.taskId,
    row.taskName,
    row.orgName,
    row.userName,
    row.ownerRunId,
  ].join(" ").toLowerCase();
  return haystack.includes(query);
}

export function RunDataPage() {
  const desk = useDesk();
  const userId = desk.user?.id ?? "";
  const [search, setSearch] = useSearchParams();
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [tab, setTab] = useState<string>(TAB_META[0].label);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [includeChildren, setIncludeChildren] = useState(false);
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

  const titles = useMemo(() => {
    const map = new Map<string, string>();
    for (const session of desk.sessions) {
      if (session.title) map.set(session.id, session.title);
    }
    return map;
  }, [desk.sessions]);

  const userNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const person of desk.users) map.set(person.id, person.name);
    return map;
  }, [desk.users]);

  const listed = useMemo(() => {
    const orgName = desk.distributor?.name ?? "";
    const needle = query.trim().toLowerCase();
    return runs
      .filter((run) => includeChildren || !run.owner_run_id)
      .map((run) => listRun(run, titles, orgName, userNames.get(run.user_id) ?? ""))
      .filter((row) => matches(row, needle));
  }, [runs, includeChildren, titles, userNames, desk.distributor?.name, query]);

  const activeTab = TAB_META.find((item) => item.label === tab) ?? TAB_META[0];
  const rows = useMemo(() => (trace ? activeTab.rows(trace) : []), [trace, activeTab]);
  const columns = useMemo(() => tableColumns(rows), [rows]);

  function chooseRun(id: string) {
    const next = new URLSearchParams(search);
    if (id) next.set("run_id", id);
    else next.delete("run_id");
    setSearch(next, { replace: true });
    setTab(TAB_META[0].label);
  }

  function idButton(id: string) {
    if (!id) return "—";
    return (
      <button type="button" className="id-link" onClick={() => chooseRun(id)}>
        {id}
      </button>
    );
  }

  return (
    <>
      <PageHead
        title="Run 表数据"
        caption="按 run 查看原始行。列表默认只放对话本身的 run；子任务（data、tool、report、open）勾选后才出现。点 run id 进入这张 run 的各表。"
      />
      {error && <div className="error">{error}</div>}
      {!userId && <div className="empty">先在左侧选一个用户。</div>}
      {userId && !runId && (
        <>
          <div className="filters">
            <label className="field grow">
              搜索
              <input
                aria-label="搜索 run"
                value={query}
                placeholder="run id、会话、用户、任务"
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <label className="check-line">
              <input
                type="checkbox"
                checked={includeChildren}
                onChange={(event) => setIncludeChildren(event.target.checked)}
              />
              包含子任务
            </label>
          </div>
          <p className="muted">{listed.length} 条 · 接口最多返回最近 100 条 run</p>
          {listed.length === 0 && <p className="empty">没有匹配的 run。</p>}
          {listed.length > 0 && (
            <DataTable
              columns={[
                { key: "id", label: "Run ID", render: (row: ListedRun) => idButton(row.id) },
                { key: "status", label: "状态", render: (row: ListedRun) => <Badge status={row.status} /> },
                { key: "runType", label: "类型" },
                {
                  key: "sessionId",
                  label: "会话 ID",
                  render: (row: ListedRun) => row.sessionId ? <span className="mono">{row.sessionId}</span> : "—",
                },
                {
                  key: "sessionName",
                  label: "会话名称",
                  wrap: true,
                  render: (row: ListedRun) => clip(row.sessionName, 80) || "—",
                },
                ...(includeChildren ? [
                  { key: "taskId", label: "任务 ID", render: (row: ListedRun) => row.taskId || "—" },
                  {
                    key: "taskName",
                    label: "任务名称",
                    wrap: true,
                    render: (row: ListedRun) => clip(row.taskName, 80) || "—",
                  },
                  { key: "ownerRunId", label: "所属 run", render: (row: ListedRun) => idButton(row.ownerRunId) },
                ] : []),
                { key: "orgName", label: "经销商" },
                { key: "userName", label: "用户" },
                {
                  key: "createdLabel",
                  label: "创建时间",
                  render: (row: ListedRun) => <span title={row.createdAt}>{row.createdLabel}</span>,
                },
                { key: "endedLabel", label: "结束时间" },
              ]}
              rows={listed}
            />
          )}
        </>
      )}
      {userId && runId && (
        <>
          <div className="actions">
            <button type="button" className="btn" onClick={() => chooseRun("")}>返回列表</button>
          </div>
          {!trace && !error && <p className="muted">加载中…</p>}
          {trace && (
            <>
              <p className="muted">
                <span className="mono">{String(trace.run.id)}</span>{" "}
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
