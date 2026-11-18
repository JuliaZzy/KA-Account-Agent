import { useEffect, useState } from "react";

import { api, postForm, withUser } from "../api";
import { NeedPartnership, useDesk } from "../desk";
import {
  lastMonth,
  liveSummary,
  orderedVsSold,
  reportCharts,
  revenueLabel,
  shortDate,
  sumBy,
  unitsLabel,
  type Fact,
} from "../format";
import { Markdown } from "../markdown-view";
import { Badge, Bars, DataTable, Metrics, PageHead } from "../ui";

type Narrative = {
  id: string;
  title: string;
  body: string;
  charts: unknown;
  confirmed_at: string | null;
};

type Report = {
  id: string;
  title: string;
  status: string;
  file_name: string;
  reporting_period: string | null;
  period_start: string | null;
  period_end: string | null;
  created_at: string | null;
  confirmed_at: string | null;
};

type FactRow = Fact & {
  status?: string;
  unit: string | null;
  source_sheet: string | null;
  source_row: number | null;
  source_header: string | null;
  channel: string | null;
};

type Extraction = { method: string; extracted_at: string | null; raw: { tables?: { sheet: string; header_row: number; currency?: string | null; columns: { column: string; role: string }[] }[] }; skipped?: string[] };

type Preview = {
  fact_count: number;
  facts: FactRow[];
  skipped: string[];
  mappings: { sheet: string; header_row: number; currency: string | null; columns: { column: string; role: string }[] }[];
};

type Order = { items: { sku: string; quantity: number; case_pack: number | null; product_name: string }[] };

export function Reports() {
  return <NeedPartnership><ReportsDesk /></NeedPartnership>;
}

function ReportsDesk() {
  const { partnership, user } = useDesk();
  const p = partnership!;
  const userId = user?.id ?? "";
  const [rows, setRows] = useState<Report[]>([]);
  const [narratives, setNarratives] = useState<Narrative[]>([]);
  const [narrativeId, setNarrativeId] = useState("");
  const [pick, setPick] = useState("");
  const [facts, setFacts] = useState<FactRow[]>([]);
  const [runs, setRuns] = useState<Extraction[]>([]);
  const [live, setLive] = useState<FactRow[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [period, setPeriod] = useState(lastMonth());
  const [title, setTitle] = useState("");
  const [matrix, setMatrix] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh(selectId?: string) {
    if (!userId) return;
    const [list, confirmed, placed, writes] = await Promise.all([
      api<Report[]>(withUser(`/api/partnerships/${p.id}/reports`, userId)),
      api<FactRow[]>(withUser(`/api/partnerships/${p.id}/facts`, userId)),
      api<Order[]>(withUser(`/api/orders?partnership_id=${encodeURIComponent(p.id)}`, userId)),
      api<Narrative[]>(withUser(`/api/partnerships/${p.id}/narratives`, userId)),
    ]);
    setRows(list);
    setNarratives(writes);
    setNarrativeId((current) => (current && writes.some((row) => row.id === current) ? current : writes[0]?.id ?? ""));
    setLive(confirmed);
    setOrders(placed);
    const id = selectId && list.some((row) => row.id === selectId) ? selectId : (pick && list.some((row) => row.id === pick) ? pick : list[0]?.id ?? "");
    setPick(id);
  }

  useEffect(() => {
    refresh().catch((err: Error) => setError(err.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.id, userId]);

  useEffect(() => {
    if (!pick) { setFacts([]); setRuns([]); return; }
    if (!userId) return;
    api<{ facts: FactRow[]; extractions: Extraction[] }>(withUser(`/api/reports/${pick}`, userId))
      .then((body) => { setFacts(body.facts); setRuns(body.extractions); })
      .catch((err: Error) => setError(err.message));
  }, [pick, userId]);

  async function analyze(save: boolean) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      if (!save) {
        setPreview(await postForm<Preview>(`/api/partnerships/${p.id}/reports/preview`, file, { period, matrix, user_id: userId }));
      } else {
        const saved = await postForm<Report>(`/api/partnerships/${p.id}/reports`, file, { period, matrix, title, user_id: userId });
        setPreview(null);
        setFile(null);
        await refresh(saved.id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not read the file");
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!pick) return;
    const out = await postFormSafe(() => api<{ confirmed: number; superseded: number }>(withUser(`/api/reports/${pick}/confirm`, userId), { method: "POST" }));
    if (!out) return;
    setToast(`Confirmed ${out.confirmed} facts${out.superseded ? `, superseded ${out.superseded}` : ""}`);
    await refresh(pick);
  }

  async function remove() {
    if (!pick) return;
    await api(withUser(`/api/reports/${pick}`, userId), { method: "DELETE" });
    setPick("");
    await refresh();
  }

  function postFormSafe<T>(run: () => Promise<T>): Promise<T | null> {
    return run().catch((err: Error) => { setError(err.message); return null; });
  }

  const current = rows.find((row) => row.id === pick);
  const narrative = narratives.find((row) => row.id === narrativeId) ?? narratives[0];
  const summary = liveSummary(live, p.currency);
  const compared = orderedVsSold(orders, summary.unitRows);

  return (
    <>
      <PageHead title="Reports" caption={`${p.brand_name} · ${p.distributor_name} — every number is read from a cell by code, sits in draft until confirmed, and a re-sent month replaces the earlier one.`} />
      {error && <div className="error">{error}</div>}
      {toast && <div className="card ok-note">{toast}</div>}
      {narratives.length > 0 && (
        <>
          <h2>Confirmed write-ups</h2>
          <p className="muted">Edited in chat and confirmed. The one you saved last is selected.</p>
          {narratives.length > 1 && (
            <label className="field">Write-up
              <select value={narrative?.id ?? ""} onChange={(event) => setNarrativeId(event.target.value)}>
                {narratives.map((row) => <option key={row.id} value={row.id}>{row.title}</option>)}
              </select>
            </label>
          )}
          {narrative && (
            <div className="card result-doc">
              <p><strong>{narrative.title}</strong> <Badge status="confirmed" /> · {shortDate(narrative.confirmed_at)}</p>
              <Markdown text={narrative.body} charts={reportCharts(narrative.charts)} prose />
            </div>
          )}
        </>
      )}
      <details className="card" open={rows.length === 0 && narratives.length === 0}>
        <summary>Upload a report</summary>
        <p className="muted">A tidy table: one header row, one row per period / product / channel, numeric columns (.xlsx or .csv).</p>
        <div className="filters">
          <label className="field grow">Report file
            <input type="file" accept=".xlsx,.xlsm,.csv" onChange={(event) => { setFile(event.target.files?.[0] ?? null); setPreview(null); }} />
          </label>
          <label className="field">Reporting period
            <input value={period} onChange={(event) => setPeriod(event.target.value)} />
          </label>
          <label className="field">Title
            <input value={title} placeholder={file?.name || "August sell-out"} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <label className="field">Month columns measure
            <select value={matrix} onChange={(event) => setMatrix(event.target.value)}>
              <option value="">(no month columns)</option>
              <option value="units">units</option>
              <option value="revenue">revenue</option>
              <option value="stock">stock</option>
            </select>
          </label>
          <button className="btn" type="button" disabled={!file || busy} onClick={() => void analyze(false)}>Preview</button>
          <button className="btn-primary" type="button" disabled={!file || busy || preview?.fact_count === 0} onClick={() => void analyze(true)}>Save as draft</button>
        </div>
        {preview && <PreviewView preview={preview} />}
      </details>
      {rows.length === 0 && narratives.length === 0 && <div className="empty">No reports yet.</div>}
      {rows.length > 0 && (
        <>
          <h2>On file</h2>
          <DataTable rows={rows} columns={[
            { key: "title", label: "Title" },
            { key: "period", label: "Period", render: (row) => row.reporting_period || `${row.period_start} → ${row.period_end}` },
            { key: "status", label: "Status", render: (row) => <Badge status={row.status} /> },
            { key: "file_name", label: "File" },
            { key: "created_at", label: "Uploaded", render: (row) => shortDate(row.created_at) },
            { key: "confirmed_at", label: "Confirmed", render: (row) => shortDate(row.confirmed_at) },
          ]} />
          <label className="field">Report
            <select value={pick} onChange={(event) => setPick(event.target.value)}>
              {rows.map((row) => <option key={row.id} value={row.id}>{row.title} · {row.reporting_period || row.period_start || ""}</option>)}
            </select>
          </label>
          {current && (
            <>
              <p><strong>{current.title}</strong> <Badge status={current.status} /> · {current.file_name} · {facts.length} facts</p>
              <div className="actions">
                {current.status === "draft" && <button className="btn-primary" type="button" onClick={() => void confirm()}>Confirm facts</button>}
                <button className="btn" type="button" onClick={() => void remove()}>Delete report</button>
              </div>
              {facts.length > 0 && (
                <DataTable rows={facts} columns={[
                  { key: "status", label: "Status" },
                  { key: "metric_key", label: "Metric" },
                  { key: "sku", label: "SKU" },
                  { key: "entity", label: "Product" },
                  { key: "channel", label: "Channel" },
                  { key: "period_start", label: "Period" },
                  { key: "numeric_value", label: "Value" },
                  { key: "unit", label: "Unit" },
                  { key: "source", label: "Source", render: (row) => `${row.source_sheet} r${row.source_row} · ${row.source_header}` },
                ]} />
              )}
              {runs.map((run, index) => (
                <details key={index} className="card">
                  <summary>How it was read · {run.method} · {shortDate(run.extracted_at)}</summary>
                  {(run.raw.tables ?? []).map((table) => (
                    <div key={table.sheet}>
                      <p><strong>{table.sheet}</strong> · header row {table.header_row + 1}{table.currency ? ` · ${table.currency}` : ""}</p>
                      <DataTable rows={table.columns} columns={[{ key: "column", label: "column" }, { key: "role", label: "role" }]} />
                    </div>
                  ))}
                  {(run.skipped ?? []).map((item) => <p className="muted" key={item}>{item}</p>)}
                </details>
              ))}
            </>
          )}
        </>
      )}
      <h2>Live numbers</h2>
      {live.length === 0 ? <p className="muted">Confirm a report to see its numbers here.</p> : (
        <>
          <Metrics items={[
            { label: "Revenue", value: revenueLabel(summary) },
            { label: "Units sold", value: unitsLabel(summary.units) },
            { label: "Months covered", value: String(summary.months) },
            { label: "SKUs seen", value: String(summary.skus) },
          ]} />
          <div className="chat-layout" style={{ height: "auto" }}>
            <div>
              {summary.revenueRows.length > 0 && (
                <div className="card">
                  <h3>Revenue by month ({summary.currency})</h3>
                  <Bars rows={sumBy(summary.revenueRows, (fact) => (fact.period_start || "").slice(0, 7))} />
                </div>
              )}
              {summary.unitRows.some((fact) => fact.channel) && (
                <div className="card">
                  <h3>Units by channel</h3>
                  <Bars rows={sumBy(summary.unitRows, (fact) => `${(fact.period_start || "").slice(0, 7)} · ${fact.channel}`)} />
                </div>
              )}
            </div>
            <div>
              {summary.unitRows.length > 0 && (
                <div className="card">
                  <h3>Units by product</h3>
                  <Bars rows={sumBy(summary.unitRows, (fact) => fact.entity || fact.sku || "").sort((a, b) => b.value - a.value).slice(0, 12)} />
                </div>
              )}
            </div>
          </div>
          {compared.length > 0 && (
            <>
              <h3>Ordered vs sold, by SKU (units; orders × case pack)</h3>
              <DataTable rows={compared} columns={[
                { key: "sku", label: "SKU" },
                { key: "product", label: "Product" },
                { key: "ordered", label: "Ordered" },
                { key: "sold", label: "Sold" },
                { key: "sellThrough", label: "Sell-through", render: (row) => row.sellThrough == null ? "—" : (
                  <span><span className="progress"><span style={{ width: `${Math.min(100, row.sellThrough * 100)}%` }} /></span>{Math.round(row.sellThrough * 100)}%</span>
                ) },
              ]} />
            </>
          )}
        </>
      )}
    </>
  );
}

function PreviewView({ preview }: { preview: Preview }) {
  return (
    <div>
      {preview.mappings.map((mapping) => (
        <div key={mapping.sheet}>
          <p><strong>{mapping.sheet}</strong> · header on row {mapping.header_row + 1}{mapping.currency ? ` · currency ${mapping.currency}` : ""}</p>
          <DataTable rows={mapping.columns} columns={[{ key: "column", label: "column" }, { key: "role", label: "role" }]} />
        </div>
      ))}
      {preview.fact_count > 0 ? <p><strong>{preview.fact_count} facts</strong> read (draft until you confirm the report)</p> : <div className="warn">No facts could be read from this file.</div>}
      {preview.facts.length > 0 && (
        <DataTable rows={preview.facts} columns={[
          { key: "metric_key", label: "Metric" },
          { key: "sku", label: "SKU" },
          { key: "entity", label: "Product" },
          { key: "channel", label: "Channel" },
          { key: "period_start", label: "Period" },
          { key: "numeric_value", label: "Value" },
          { key: "unit", label: "Unit" },
          { key: "source", label: "Source", render: (row) => `${row.source_sheet} r${row.source_row} · ${row.source_header}` },
        ]} />
      )}
      {preview.skipped.length > 0 && (
        <details><summary>{preview.skipped.length} cell(s) or sheet(s) skipped</summary>{preview.skipped.map((item) => <p className="muted" key={item}>{item}</p>)}</details>
      )}
    </div>
  );
}
