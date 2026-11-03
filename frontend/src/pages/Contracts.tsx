import { useEffect, useState } from "react";

import { api, postForm } from "../api";
import { NeedPartnership, useDesk } from "../desk";
import { shortDate, trimNum } from "../format";
import { Badge, DataTable, Metrics, PageHead, Tabs } from "../ui";

type Contract = {
  id: string;
  title: string | null;
  status: string;
  contract_type: string | null;
  file_name: string | null;
  term_start_date: string | null;
  term_end_date: string | null;
  auto_renewal: boolean | null;
  exclusivity_type: string | null;
  territory_text: string | null;
  annual_sales_target: Record<string, unknown> | null;
  annual_sales_target_currency: string | null;
};

type Tier = { from_container: number | null; to_container: number | null; discount_percent: number | null; notes: string | null };
type DiscountRule = { title: string | null; section_reference: string | null; applies_per: string | null; basis: string | null; tiers: Tier[]; source_clause_text: string | null };
type Moq = { quantity: number | null; unit: string | null; currency: string | null; applies_per: string | null; product_scope: string | null; sku: string | null; source_clause_text: string | null };
type Territory = { country_code: string; kind: string };
type Evidence = { field: string; page: number | null; section_hint: string | null; quote: string };
type Rule = { name: string; rule_type: string; severity: string; rule_config: Record<string, unknown>; is_active: boolean };
type Run = { extracted_at: string | null; model: string | null; confidence_score: number | null; extraction_notes: string | null; skipped: string[] | null; raw: unknown };

type Detail = {
  contract: Contract;
  terms: { discount_rules: DiscountRule[]; moqs: Moq[]; price_list: Record<string, unknown>[]; territories: Territory[]; evidence: Evidence[] };
  rules: Rule[];
  extractions: Run[];
  next_discount: { percent: number; note: string } | null;
};

export function Contracts() {
  return <NeedPartnership><ContractsDesk /></NeedPartnership>;
}

function ContractsDesk() {
  const { partnership } = useDesk();
  const p = partnership!;
  const [rows, setRows] = useState<Contract[]>([]);
  const [pick, setPick] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [tab, setTab] = useState("Terms");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function loadList(selectId?: string) {
    const list = await api<Contract[]>(`/api/partnerships/${p.id}/contracts`);
    setRows(list);
    const id = selectId && list.some((row) => row.id === selectId) ? selectId : list[0]?.id ?? "";
    setPick(id);
  }

  useEffect(() => {
    loadList().catch((err: Error) => setError(err.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.id]);

  useEffect(() => {
    if (!pick) { setDetail(null); return; }
    api<Detail>(`/api/contracts/${pick}`).then(setDetail).catch((err: Error) => setError(err.message));
  }, [pick]);

  async function importFile() {
    if (!file) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await postForm<{ id: string; title: string; discount_rules: number; po_rules: number; skipped: string[] }>(
        `/api/partnerships/${p.id}/contracts`,
        file,
        { title },
      );
      setNotice(`Imported ${saved.title}: ${saved.discount_rules} discount rule(s), ${saved.po_rules} PO rule(s).${saved.skipped.length ? ` Skipped: ${saved.skipped.join("; ")}` : ""}`);
      setFile(null);
      setTitle("");
      await loadList(saved.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "import failed");
    } finally {
      setBusy(false);
    }
  }

  const c = detail?.contract;
  const minOrder = detail?.terms.moqs.find((m) => m.quantity && (m.applies_per || "").toLowerCase().includes("order") && (m.currency || ["USD", "EUR", "CNY", "GBP", "SEK"].includes((m.unit || "").toUpperCase())));
  const excluded = detail?.terms.territories.filter((t) => t.kind === "excluded").length ?? 0;
  const allowed = detail?.terms.territories.filter((t) => t.kind === "allowed").length ?? 0;

  return (
    <>
      <PageHead title="Contracts" caption={`${p.brand_name} · ${p.distributor_name} — terms the code enforces on every order, and where each one came from.`} />
      {error && <div className="error">{error}</div>}
      {notice && <div className="card ok-note">{notice}</div>}
      <details className="card" open={rows.length === 0}>
        <summary>File an agreement</summary>
        <p className="muted">A PDF, Word file, or text — the model copies the terms, the code derives the PO rules. Or a JSON extraction. Stored in full; the previous active contract is superseded.</p>
        <div className="filters">
          <label className="field grow">Agreement
            <input type="file" accept=".pdf,.docx,.txt,.md,.json" onChange={(event) => setFile(event.target.files?.[0] ?? null)} />
          </label>
          <label className="field grow">Title
            <input value={title} placeholder="Distribution Agreement 2027" onChange={(event) => setTitle(event.target.value)} />
          </label>
          <button className="btn-primary" type="button" disabled={!file || busy} onClick={() => void importFile()}>{busy ? "Reading…" : "Import"}</button>
        </div>
      </details>
      {rows.length === 0 && <div className="empty">No contract on file for this partnership yet.</div>}
      {rows.length > 0 && (
        <label className="field">Contract
          <select value={pick} onChange={(event) => setPick(event.target.value)}>
            {rows.map((row) => (
              <option key={row.id} value={row.id}>
                {(row.title || row.id)}{row.status === "active" ? "" : ` (${row.status})`}
              </option>
            ))}
          </select>
        </label>
      )}
      {c && detail && (
        <>
          <h2>{c.title || c.id} <Badge status={c.status} /></h2>
          <p className="muted">{caption(c)}</p>
          <Metrics items={[
            { label: "Next order's discount", value: detail.next_discount ? `${trimNum(detail.next_discount.percent)}%` : "—", hint: detail.next_discount?.note },
            { label: "Discount schedules", value: String(detail.terms.discount_rules.length) },
            { label: "Minimum order", value: minOrder?.quantity != null ? `${minOrder.unit || minOrder.currency} ${trimNum(minOrder.quantity)}` : "—" },
            { label: "Territories allowed", value: String(allowed) },
            { label: "PO rules derived", value: String(detail.rules.length) },
          ]} />
          {excluded > 0 && <p className="muted">{excluded} territor{excluded === 1 ? "y" : "ies"} excluded</p>}
          <Tabs labels={["Terms", "PO rules", "Evidence", "Extractions"]} value={tab} onChange={setTab} />
          {tab === "Terms" && <Terms detail={detail} />}
          {tab === "PO rules" && (
            <>
              <p className="muted">What the code-check enforces because of this contract. Each rule carries contract_id, so an approver can follow it back to the clause.</p>
              {detail.rules.length === 0 ? <p className="muted">No rules derived.</p> : (
                <DataTable
                  rows={detail.rules}
                  columns={[
                    { key: "name", label: "Rule" },
                    { key: "rule_type", label: "Type" },
                    { key: "severity", label: "Severity" },
                    { key: "rule_config", label: "Config", wrap: true, render: (row) => JSON.stringify(row.rule_config) },
                    { key: "is_active", label: "Active", render: (row) => row.is_active ? "yes" : "no" },
                  ]}
                />
              )}
            </>
          )}
          {tab === "Evidence" && (
            detail.terms.evidence.length === 0 ? <p className="muted">No evidence recorded.</p> : (
              <DataTable rows={detail.terms.evidence} columns={[
                { key: "field", label: "Field" },
                { key: "page", label: "Page" },
                { key: "section_hint", label: "Section" },
                { key: "quote", label: "Quote", wrap: true },
              ]} />
            )
          )}
          {tab === "Extractions" && detail.extractions.slice().reverse().map((run, index) => (
            <div className="card" key={index}>
              <strong>{shortDate(run.extracted_at)}</strong> · model {run.model || "—"} · confidence {run.confidence_score ?? "—"}
              {run.extraction_notes && <p className="muted">{run.extraction_notes}</p>}
              {run.skipped && run.skipped.length > 0 && <div className="warn">Not mapped (reported, never guessed): {run.skipped.join("; ")}</div>}
              <details><summary>Raw extraction</summary><pre>{JSON.stringify(run.raw, null, 2)}</pre></details>
            </div>
          ))}
        </>
      )}
    </>
  );
}

function caption(c: Contract): string {
  return [
    c.contract_type?.replaceAll("_", " "),
    c.file_name,
    (c.term_start_date || c.term_end_date) ? `term ${c.term_start_date || "?"} → ${c.term_end_date || "?"}` : null,
    c.auto_renewal === true ? "auto-renews" : c.auto_renewal === false ? "no auto-renewal" : null,
    c.exclusivity_type && (c.exclusivity_type.toLowerCase().includes("exclusiv") ? c.exclusivity_type : `${c.exclusivity_type} exclusivity`),
  ].filter(Boolean).join(" · ");
}

function Terms({ detail }: { detail: Detail }) {
  const c = detail.contract;
  return (
    <>
      {c.territory_text && <p><strong>Territory</strong> {c.territory_text}</p>}
      <p>
        {detail.terms.territories.map((t) => (
          <span key={`${t.kind}-${t.country_code}`} className={`badge ${t.kind === "allowed" ? "ok" : "bad"}`} style={{ marginRight: 6 }}>
            {t.country_code}{t.kind === "excluded" ? " excluded" : ""}
          </span>
        ))}
      </p>
      {c.annual_sales_target && <p><strong>Annual sales target</strong> {JSON.stringify(c.annual_sales_target)} {c.annual_sales_target_currency || ""}</p>}
      <h3>Discount schedules</h3>
      {detail.terms.discount_rules.length === 0 && <p className="muted">None extracted.</p>}
      {detail.terms.discount_rules.map((rule, index) => (
        <div className="card" key={index}>
          <strong>{rule.title || "Discount rule"}</strong>
          {rule.section_reference ? ` · §${rule.section_reference}` : ""}
          <div className="muted">{(rule.applies_per || "").replaceAll("_", " ")} · {(rule.basis || "").replaceAll("_", " ")}</div>
          <DataTable
            rows={rule.tiers}
            columns={[
              { key: "containers", label: "Containers", render: (tier) => `${tier.from_container || 1} – ${tier.to_container ?? "∞"}` },
              { key: "discount", label: "Discount", render: (tier) => tier.discount_percent == null ? "—" : `${trimNum(tier.discount_percent)}%` },
              { key: "notes", label: "Notes" },
            ]}
          />
          {rule.source_clause_text && <p className="muted">“{rule.source_clause_text}”</p>}
        </div>
      ))}
      {detail.terms.moqs.length > 0 && (
        <>
          <h3>Minimum order quantities</h3>
          <DataTable rows={detail.terms.moqs} columns={[
            { key: "quantity", label: "Quantity", render: (row) => row.quantity == null ? "—" : trimNum(row.quantity) },
            { key: "unit", label: "Unit", render: (row) => row.unit || row.currency || "" },
            { key: "applies_per", label: "Applies per", render: (row) => (row.applies_per || "").replaceAll("_", " ") },
            { key: "scope", label: "Scope", render: (row) => row.product_scope || row.sku || "all" },
            { key: "source_clause_text", label: "Clause", wrap: true },
          ]} />
        </>
      )}
      {detail.terms.price_list.length > 0 && (
        <>
          <h3>Price list</h3>
          <DataTable
            rows={detail.terms.price_list}
            columns={Object.keys(detail.terms.price_list[0]).map((key) => ({ key, label: key }))}
          />
        </>
      )}
    </>
  );
}
