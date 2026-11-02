/** Pure desk formatting. The page renders these; it does not recompute prices or rules. */

export type Activity = {
  id: string;
  title: string;
  detail: string;
  state: "running" | "done";
};

function textField(raw: unknown, key: string): string {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return "";
  const value = (raw as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

/** One line per public run event that carries `display`. The page does not rename tools. */
export function activitiesFromEvents(raw: unknown, status?: string): Activity[] {
  if (!Array.isArray(raw)) return [];
  const rows = raw.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as { sequence?: unknown; payload_json?: unknown };
    const sequence = Number(row.sequence);
    const title = textField(row.payload_json, "display");
    if (!Number.isInteger(sequence) || sequence <= 0 || !title) return [];
    return [{ id: String(sequence), title, detail: "", state: "done" as const, sequence }];
  });
  rows.sort((a, b) => a.sequence - b.sequence);
  const moving = status === "queued" || status === "running";
  return rows.map((row, index) => ({
    id: row.id,
    title: row.title,
    detail: row.detail,
    state: moving && index === rows.length - 1 ? "running" as const : "done" as const,
  }));
}

/** Steps from `GET /runs/{id}/trace`. A row without an id or a name is dropped.

The harness names a model step `model`. A desk turn stores its label on the row,
and that label is what the list shows.
*/
export function stepsFromTrace(raw: unknown): Activity[] {
  if (!Array.isArray(raw)) return [];
  const rows = raw.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as {
      id?: unknown;
      name?: unknown;
      status?: unknown;
      step_index?: unknown;
      input_json?: unknown;
      output_json?: unknown;
    };
    const id = String(row.id ?? "").trim();
    const name = String(row.name ?? "").trim();
    if (!id || !name) return [];
    const index = Number(row.step_index);
    const status = String(row.status ?? "");
    const title = textField(row.output_json, "title") || textField(row.input_json, "title") || name;
    const detail = textField(row.output_json, "detail") || textField(row.input_json, "detail")
      || (status === "failed" ? "失败" : "");
    return [{
      id,
      title,
      detail,
      index: Number.isInteger(index) ? index : 0,
      state: status === "running" ? "running" as const : "done" as const,
    }];
  });
  rows.sort((a, b) => a.index - b.index);
  return rows.map((row) => ({ id: row.id, title: row.title, detail: row.detail, state: row.state }));
}

export type Partnership = {
  id: string;
  brand_id: string;
  brand_name: string;
  distributor_name: string;
  ship_to_country: string | null;
  currency: string;
};

export type HistoryMessage = { role: string; content?: string };

export type Citation = { n: number; source: string };

export type DataQueryMark = { logId: number; feedback?: "good" | "bad" };

/** The numbered file-and-page lines under a brand answer. Bad rows are dropped. */
export function citationSources(raw: unknown): Citation[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const sources = raw.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as { n?: unknown; source?: unknown };
    const n = Number(row.n);
    const source = String(row.source ?? "").trim();
    return Number.isInteger(n) && source ? [{ n, source }] : [];
  });
  return sources.length ? sources : undefined;
}

export type ReportChart = {
  id: string;
  title: string;
  subtitle: string;
  option: Record<string, unknown>;
};

/** Charts whose `echarts_option` can be passed to ECharts. Specs without one are dropped. */
export function reportCharts(raw: unknown): ReportChart[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const charts: ReportChart[] = [];
  for (const item of raw) {
    if (charts.length >= 4) break;
    if (!item || typeof item !== "object") continue;
    const row = item as { id?: unknown; title?: unknown; subtitle?: unknown; echarts_option?: unknown };
    const title = String(row.title ?? "").trim();
    const option = row.echarts_option;
    if (!title || !option || typeof option !== "object" || Array.isArray(option)) continue;
    const given = String(row.id ?? "").trim();
    const used = new Set(charts.map((chart) => chart.id));
    let id = given;
    if (!id || used.has(id)) {
      let n = 1;
      while (used.has(`chart_${String(n).padStart(3, "0")}`)) n += 1;
      id = `chart_${String(n).padStart(3, "0")}`;
    }
    charts.push({ id, title, subtitle: String(row.subtitle ?? "").trim(), option: option as Record<string, unknown> });
  }
  return charts.length ? charts : undefined;
}

export type ContractFiling = {
  contract_id?: string;
  title?: string | null;
  file_name?: string | null;
  contract_type?: string | null;
  term_start_date?: string | null;
  term_end_date?: string | null;
  auto_renewal?: boolean | null;
  exclusivity_type?: string | null;
  territory_text?: string | null;
  discount_rules?: {
    title?: string | null;
    section_reference?: string | null;
    applies_per?: string | null;
    basis?: string | null;
    source_clause_text?: string | null;
    tiers?: {
      from_container?: number | null;
      to_container?: number | null;
      discount_percent?: number | null;
      notes?: string | null;
    }[];
  }[];
  moqs?: Record<string, unknown>[];
  territories?: { country_code?: string; kind?: string }[];
  price_list?: Record<string, unknown>[];
  derived_rules?: { name?: string; rule_type?: string; severity?: string; rule_config?: Record<string, unknown> }[];
  skipped?: string[];
  pending_id?: string;
  confirmed?: boolean;
};

/** The filing table stored on a contract turn. Empty objects are dropped. */
export function contractFiling(raw: unknown): ContractFiling | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const row = raw as ContractFiling;
  const named = String(row.title || row.file_name || row.contract_id || "").trim();
  const hasTerms = Array.isArray(row.discount_rules) || Array.isArray(row.moqs) || Array.isArray(row.derived_rules);
  if (!named && !hasTerms) return undefined;
  return row;
}

export type ResultPane =
  | { id: string; kind: "order"; label: string; pending?: boolean }
  | { id: string; kind: "report"; label: string; text: string; charts?: ReportChart[]; pending?: boolean }
  | { id: string; kind: "contract"; label: string; filing: ContractFiling | null; pending?: boolean };

function numbered(name: string, index: number, count: number): string {
  return count > 1 ? `${name} ${index + 1}` : name;
}

/** One tab per artifact in this chat. A live task is the last tab of its kind. */
export function resultPanes(input: {
  hasOrder: boolean;
  reports: { id: string; text: string; charts?: ReportChart[] }[];
  contracts: { id: string; filing: ContractFiling }[];
  live: { kind: "report" | "contract" | "po"; text: string } | null;
  pending?: ("po" | "report" | "contract")[];
}): ResultPane[] {
  const waiting = new Set(input.pending ?? []);
  const reports = [...input.reports];
  if (input.live?.kind === "report") reports.push({ id: "report-live", text: input.live.text });
  else if (waiting.has("report")) reports.push({ id: "report-live", text: "" });
  const contracts = [...input.contracts];
  const pendingContract = input.live?.kind === "contract" || waiting.has("contract");
  const pendingOrder = (input.live?.kind === "po" || waiting.has("po")) && !input.hasOrder;
  const panes: ResultPane[] = [];
  if (input.hasOrder || pendingOrder) {
    panes.push({ id: pendingOrder ? "order-live" : "order", kind: "order", label: "采购单", pending: pendingOrder });
  }
  reports.forEach((report, index) => {
    panes.push({
      id: report.id,
      kind: "report",
      label: numbered("报告", index, reports.length),
      text: report.text,
      charts: report.charts,
      pending: report.id === "report-live" && !report.text,
    });
  });
  contracts.forEach((contract, index) => {
    panes.push({
      id: contract.id,
      kind: "contract",
      label: numbered("合同", index, contracts.length + (pendingContract ? 1 : 0)),
      filing: contract.filing,
    });
  });
  if (pendingContract) {
    panes.push({
      id: "contract-live",
      kind: "contract",
      label: numbered("合同", contracts.length, contracts.length + 1),
      filing: null,
      pending: true,
    });
  }
  return panes;
}

/** The one line a chat bubble shows instead of the artifact itself. */
export function resultLink(label: string): string {
  const match = /^(采购单|报告|合同)(?:\s+(\d+))?$/.exec(label);
  if (!match) return `查看${label}`;
  const name = match[1] === "采购单" ? "查看采购单" : `查看生成的${match[1]}`;
  return match[2] ? `${name} ${match[2]}` : name;
}

/** Titles of public pages consulted while drafting. Empty and non-strings are dropped. */
export function webNotes(raw: unknown): string[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const notes = raw.map((item) => String(item ?? "").trim()).filter(Boolean).slice(0, 4);
  return notes.length ? notes : undefined;
}

/** The query a data answer can be rated against. Missing or junk ids are dropped. */
export function dataQueryMark(raw: unknown): DataQueryMark | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const row = raw as { log_id?: unknown; feedback?: unknown };
  const logId = Number(row.log_id);
  if (!Number.isInteger(logId) || logId <= 0) return undefined;
  const feedback = row.feedback === "good" || row.feedback === "bad" ? row.feedback : undefined;
  return { logId, feedback };
}

const SKIP_HISTORY = new Set(["(message)", "(confirm)"]);
const TAG_LABEL: Record<string, string> = {
  po: "PO",
  contract: "CONTRACT",
  report: "REPORT",
  brand: "BRAND",
  data: "DATA",
};

export function money(currency: string | null | undefined, n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const formatted = n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${currency ?? ""} ${formatted}`.trim();
}

export function trimNum(n: number): string {
  if (Number.isInteger(n)) return String(n);
  return String(n);
}

export function shortDate(ts: string | null | undefined): string {
  if (!ts) return "—";
  return ts.slice(0, 10);
}

export function clip(text: string, n: number): string {
  const flat = text.replace(/\n/g, " ");
  return flat.length > n ? `${flat.slice(0, n)}…` : flat;
}

export function greeting(p: Pick<Partnership, "distributor_name" | "brand_name">): string {
  return `Hi — I can place an order for ${p.distributor_name} with ${p.brand_name}. What would you like, and how many cases?`;
}

export function tagLabel(tags: string[] | null | undefined): string {
  return (tags ?? []).filter(Boolean).map((t) => TAG_LABEL[t] ?? t.toUpperCase()).join(" · ");
}

export function chatTitle(
  session: { title?: string | null; history?: HistoryMessage[] },
  turns?: { role: string; text?: string }[],
): string {
  const titled = (session.title || "").trim();
  if (titled) return clip(titled, 40);
  for (const t of turns ?? []) {
    const text = (t.text || "").trim();
    if (t.role === "user" && text) return clip(text, 36);
  }
  for (const m of session.history ?? []) {
    const text = String(m.content || "").trim();
    if (m.role === "user" && text && !SKIP_HISTORY.has(text)) return clip(text, 36);
  }
  return "New chat";
}

export type DraftLine = {
  name: string;
  sku: string | null;
  reference: string;
  cases: number;
  case_price: number | null;
  line_total: number | null;
  moq_cases: number | null;
  ambiguous: boolean;
  unresolved: boolean;
  below_moq: boolean;
  missing_price: boolean;
  partial_layer: boolean;
  cases_to_full_layer: number | null;
};

export function lineFlag(line: DraftLine): string {
  if (line.ambiguous) return "which one?";
  if (line.unresolved) return "not found";
  if (line.below_moq) return `min ${line.moq_cases}`;
  if (line.missing_price) return "no price";
  if (line.partial_layer) return `+${line.cases_to_full_layer} = full layer`;
  return "";
}

export type Discount = { kind: string; value?: number | null; source?: string | null } | null;

export function discountText(discount: Discount, currency: string): string {
  if (!discount) return "not yet decided";
  let text = "none";
  if (discount.kind !== "none") {
    text = discount.kind === "percent" ? `${trimNum(discount.value ?? 0)}%` : money(currency, discount.value);
  }
  if (discount.source === "contract") text += " · per contract";
  return text;
}

export function lastMonth(today = new Date()): string {
  const first = new Date(today.getFullYear(), today.getMonth(), 1);
  const prev = new Date(first.getFullYear(), first.getMonth() - 1, 1);
  return `${prev.getFullYear()}-${String(prev.getMonth() + 1).padStart(2, "0")}`;
}

export type Fact = {
  fact_type: string;
  metric_key: string;
  numeric_value: number | null;
  currency: string | null;
  period_start: string | null;
  sku: string | null;
  entity: string | null;
  channel: string | null;
};

export function liveSummary(facts: Fact[], fallbackCurrency: string) {
  const sales = facts.filter((f) => f.fact_type === "sales");
  const revenue = sales.filter((f) => f.metric_key === "revenue");
  const units = sales.filter((f) => f.metric_key === "units_sold");
  const months = new Set(facts.map((f) => (f.period_start || "").slice(0, 7)).filter(Boolean));
  const skus = new Set(sales.map((f) => f.sku).filter(Boolean));
  const sum = (rows: Fact[]) => rows.reduce((acc, f) => acc + (f.numeric_value || 0), 0);
  return {
    currency: revenue.map((f) => f.currency).find(Boolean) || fallbackCurrency,
    revenue: revenue.length ? sum(revenue) : null,
    units: units.length ? sum(units) : null,
    months: months.size,
    skus: skus.size,
    revenueRows: revenue,
    unitRows: units,
    stockRows: facts.filter((f) => f.metric_key === "stock_on_hand"),
  };
}

export function sumBy(rows: Fact[], key: (f: Fact) => string): { label: string; value: number }[] {
  const map = new Map<string, number>();
  for (const f of rows) {
    const label = key(f);
    if (!label) continue;
    map.set(label, (map.get(label) || 0) + (f.numeric_value || 0));
  }
  return [...map.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([label, value]) => ({ label, value }));
}

export type OrderItem = { sku: string; quantity: number; case_pack: number | null; product_name: string };

export function orderedVsSold(orders: { items: OrderItem[] }[], units: Fact[]) {
  const ordered = new Map<string, number>();
  const names = new Map<string, string>();
  for (const order of orders) {
    for (const item of order.items) {
      ordered.set(item.sku, (ordered.get(item.sku) || 0) + item.quantity * (item.case_pack || 1));
      names.set(item.sku, item.product_name);
    }
  }
  const sold = new Map<string, number>();
  const entity = new Map<string, string>();
  for (const fact of units) {
    if (!fact.sku) continue;
    sold.set(fact.sku, (sold.get(fact.sku) || 0) + (fact.numeric_value || 0));
    if (fact.entity) entity.set(fact.sku, fact.entity);
  }
  if (ordered.size === 0 || units.length === 0) return [];
  return [...new Set([...ordered.keys(), ...sold.keys()])].sort().map((sku) => {
    const placed = ordered.get(sku) || 0;
    const moved = sold.get(sku) || 0;
    return {
      sku,
      product: names.get(sku) || entity.get(sku) || "",
      ordered: placed,
      sold: moved,
      sellThrough: placed ? moved / placed : null,
    };
  });
}

export function revenueLabel(summary: { currency: string; revenue: number | null }): string {
  if (summary.revenue == null) return "—";
  return `${summary.currency} ${summary.revenue.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

export function unitsLabel(n: number | null): string {
  if (n == null) return "—";
  return n.toLocaleString("en-US", { maximumFractionDigits: 0 });
}
