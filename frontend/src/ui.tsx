import { useEffect, useRef, type ReactNode } from "react";
import * as echarts from "echarts";

import type { ReportChart } from "./format";

const TONE: Record<string, string> = {
  active: "ok",
  approved: "ok",
  confirmed: "ok",
  submitted: "ok",
  distilled: "ok",
  completed: "ok",
  draft: "wait",
  uploaded: "wait",
  gathering: "wait",
  confirm: "wait",
  pending_approval: "wait",
  superseded: "mute",
  archived: "mute",
  failed: "bad",
  cancelled: "bad",
};

export function Badge({ status }: { status: string | null | undefined }) {
  return <span className={`badge ${TONE[status || ""] || "info"}`}>{status || "—"}</span>;
}

export function Metrics({ items }: { items: { label: string; value: string; hint?: string }[] }) {
  return (
    <div className="metrics">
      {items.map((item) => (
        <div className="metric" key={item.label} title={item.hint}>
          <div className="metric-label">{item.label}</div>
          <div className="metric-value">{item.value}</div>
        </div>
      ))}
    </div>
  );
}

export function Tabs({ labels, value, onChange }: { labels: string[]; value: string; onChange: (value: string) => void }) {
  return (
    <div className="tabs" role="tablist">
      {labels.map((label) => (
        <button
          key={label}
          type="button"
          role="tab"
          aria-selected={value === label}
          className={value === label ? "tab on" : "tab"}
          onClick={() => onChange(label)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function DataTable<T>({
  columns,
  rows,
}: {
  columns: { key: string; label: string; wrap?: boolean; render?: (row: T) => ReactNode }[];
  rows: T[];
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} className={column.wrap ? "wrap" : ""}>{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column.key} className={column.wrap ? "wrap" : ""}>
                  {column.render ? column.render(row) : formatCell((row as Record<string, unknown>)[column.key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCell(value: unknown): ReactNode {
  if (value == null || value === "") return "—";
  if (Array.isArray(value)) return value.join(", ") || "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function PageHead({ title, caption }: { title: string; caption?: string }) {
  return (
    <header className="page-head">
      <h1>{title}</h1>
      {caption && <p>{caption}</p>}
    </header>
  );
}

export function ReportFigure({ chart }: { chart: ReportChart }) {
  const node = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = node.current;
    if (!el) return;
    const view = echarts.init(el);
    view.setOption(chart.option as echarts.EChartsOption);
    const observer = new ResizeObserver(() => view.resize());
    observer.observe(el);
    return () => {
      observer.disconnect();
      view.dispose();
    };
  }, [chart]);
  return (
    <figure className="chart-block">
      <figcaption>
        {chart.title}
        {chart.subtitle ? <span className="chart-sub">{chart.subtitle}</span> : null}
      </figcaption>
      <div className="chart-echarts" ref={node} />
    </figure>
  );
}

export function Bars({ rows }: { rows: { label: string; value: number }[] }) {
  const max = Math.max(1, ...rows.map((row) => row.value));
  return (
    <div className="bars">
      {rows.map((row) => (
        <div className="bar-row" key={row.label}>
          <span>{row.label}</span>
          <div className="bar-track"><div className="bar-fill" style={{ width: `${(row.value / max) * 100}%` }} /></div>
          <span>{row.value.toLocaleString("en-US")}</span>
        </div>
      ))}
    </div>
  );
}
