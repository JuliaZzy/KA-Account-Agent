import { describe, expect, it } from "vitest";

import {
  chatTitle,
  citationSources,
  dataQueryMark,
  contractFiling,
  reportCharts,
  resultLink,
  resultPanes,
  webNotes,
  discountText,
  greeting,
  lastMonth,
  lineFlag,
  liveSummary,
  money,
  orderedVsSold,
  revenueLabel,
  sumBy,
  unitsLabel,
  activitiesFromEvents,
  stepsFromTrace,
} from "./format";

describe("desk formatters", () => {
  it("formats money the way the order panel did", () => {
    expect(money("USD", 1000)).toBe("USD 1,000.00");
    expect(money("USD", null)).toBe("—");
  });

  it("keeps report charts that carry an echarts option", () => {
    const option = { series: [{ type: "line", data: [1, 2] }] };
    expect(reportCharts([{
      id: "chart_002",
      title: "已确认动销",
      subtitle: "近五个报告期",
      echarts_option: option,
    }, {
      title: " ",
      echarts_option: option,
    }, {
      title: "缺配置",
    }, {
      title: "没有编号",
      echarts_option: option,
    }])).toEqual([
      { id: "chart_002", title: "已确认动销", subtitle: "近五个报告期", option },
      { id: "chart_001", title: "没有编号", subtitle: "", option },
    ]);
    expect(reportCharts(null)).toBeUndefined();
    expect(webNotes([" Black Friday ", "", 618])).toEqual(["Black Friday", "618"]);
  });

  it("builds a tab for each order, report, and contract, and keeps a live one last", () => {
    const filing = contractFiling({ title: "2026 协议", file_name: "a.pdf", discount_rules: [] });
    expect(filing?.title).toBe("2026 协议");
    expect(contractFiling({})).toBeUndefined();
    expect(resultPanes({
      hasOrder: true,
      reports: [{ id: "report-2", text: "九月", charts: [] }, { id: "report-4", text: "十月" }],
      contracts: [{ id: "contract-3", filing: filing! }],
      live: { kind: "report", text: "正在写" },
    }).map((pane) => pane.label)).toEqual(["采购单", "报告 1", "报告 2", "报告 3", "合同"]);
    expect(resultPanes({ hasOrder: false, reports: [], contracts: [], live: null })).toEqual([]);
    expect(resultPanes({
      hasOrder: false, reports: [], contracts: [], live: { kind: "po", text: "" },
    }).map((pane) => pane.id)).toEqual(["order-live"]);
    expect(resultPanes({
      hasOrder: false, reports: [], contracts: [], live: null, pending: ["po", "report", "contract"],
    }).map((pane) => pane.id)).toEqual(["order-live", "report-live", "contract-live"]);
    expect(resultLink("报告")).toBe("查看生成的报告");
    expect(resultLink("报告 2")).toBe("查看生成的报告 2");
    expect(resultLink("合同")).toBe("查看生成的合同");
    expect(resultLink("采购单")).toBe("查看采购单");
  });

  it("keeps a data-query rating only when the log id is a positive integer", () => {
    expect(dataQueryMark({ log_id: 4, feedback: "bad" })).toEqual({ logId: 4, feedback: "bad" });
    expect(dataQueryMark({ log_id: 4, feedback: "maybe" })).toEqual({ logId: 4, feedback: undefined });
    expect(dataQueryMark({ log_id: 0 })).toBeUndefined();
    expect(dataQueryMark(null)).toBeUndefined();
  });

  it("titles a chat from the session title, else the first user line", () => {
    expect(chatTitle({ title: "10 cases fish oil" })).toBe("10 cases fish oil");
    expect(chatTitle({ title: null, history: [{ role: "user", content: "hello there" }] })).toBe("hello there");
    expect(chatTitle({ title: null, history: [{ role: "user", content: "(confirm)" }] })).toBe("New chat");
  });

  it("keeps numbered file citations and drops a blank one", () => {
    expect(citationSources([
      { n: 1, source: "品牌手册.pdf，第3页" },
      { n: 2, source: "  " },
      { n: "3", source: "价目表.pdf，第1页" },
    ])).toEqual([
      { n: 1, source: "品牌手册.pdf，第3页" },
      { n: 3, source: "价目表.pdf，第1页" },
    ]);
    expect(citationSources(undefined)).toBeUndefined();
  });

  it("greets with the partnership names", () => {
    expect(greeting({ distributor_name: "Nordic", brand_name: "Aurora" })).toContain("Nordic");
    expect(greeting({ distributor_name: "Nordic", brand_name: "Aurora" })).toContain("What would you like");
  });

  it("flags a draft line and describes a contract discount", () => {
    expect(lineFlag({
      name: "Serum", sku: null, reference: "the serum", cases: 10, case_price: null, line_total: null,
      moq_cases: null, ambiguous: true, unresolved: false, below_moq: false, missing_price: false,
      partial_layer: false, cases_to_full_layer: null,
    })).toBe("which one?");
    expect(discountText(null, "USD")).toBe("not yet decided");
    expect(discountText({ kind: "percent", value: 3, source: "contract" }, "USD")).toBe("3% · per contract");
    expect(discountText({ kind: "none" }, "USD")).toBe("none");
  });

  it("rolls the default reporting period back one month", () => {
    expect(lastMonth(new Date(2026, 9, 15))).toBe("2026-09");
    expect(lastMonth(new Date(2026, 0, 2))).toBe("2025-12");
  });

  it("sums confirmed sell-out the way the reports page did", () => {
    const facts = [
      fact("units_sold", "FO-100", 120, "2025-08-01"),
      fact("revenue", "FO-100", 1200, "2025-08-01"),
      fact("units_sold", "SR-200", 30, "2025-08-01"),
      fact("revenue", "SR-200", 1500, "2025-08-01"),
    ];
    const summary = liveSummary(facts, "USD");
    expect(revenueLabel(summary)).toBe("USD 2,700");
    expect(unitsLabel(summary.units)).toBe("150");
    expect(summary.months).toBe(1);
    expect(summary.skus).toBe(2);
    expect(sumBy(summary.revenueRows, (f) => (f.period_start || "").slice(0, 7))).toEqual([
      { label: "2025-08", value: 2700 },
    ]);
  });

  it("compares ordered units with reported sell-out", () => {
    const rows = orderedVsSold(
      [{ items: [{ sku: "FO-100", quantity: 10, case_pack: 12, product_name: "Fish Oil" }] }],
      [fact("units_sold", "FO-100", 60, "2025-08-01")],
    );
    expect(rows).toEqual([{ sku: "FO-100", product: "Fish Oil", ordered: 120, sold: 60, sellThrough: 0.5 }]);
  });

  it("paints a run timeline from event display text", () => {
    expect(activitiesFromEvents([
      { sequence: 2, event_type: "model.started", payload_json: { display: "正在思考", purpose: "hidden" } },
      { sequence: 1, event_type: "run.created", payload_json: { display: "已开始" } },
      { sequence: 3, event_type: "step.started", payload_json: {} },
    ], "running")).toEqual([
      { id: "1", title: "已开始", detail: "", state: "done" },
      { id: "2", title: "正在思考", detail: "", state: "running" },
    ]);
    expect(activitiesFromEvents([
      { sequence: 4, event_type: "tool.completed", payload_json: { tool_name: "report.draft" } },
    ], "running")).toEqual([
      { id: "4", title: "已完成 report.draft", detail: "", state: "done" },
    ]);
    expect(activitiesFromEvents(null)).toEqual([]);
  });

  it("reads a run trace as the step list", () => {
    expect(stepsFromTrace([
      { id: "step_b", name: "echo", status: "succeeded", step_index: 2 },
      { id: "step_a", name: "model", status: "running", step_index: 1 },
      { id: "step_c", name: "", status: "failed", step_index: 3 },
      { id: "step_d", name: "send_email", status: "failed", step_index: 4 },
    ])).toEqual([
      { id: "step_a", title: "model", detail: "", state: "running" },
      { id: "step_b", title: "echo", detail: "", state: "done" },
      { id: "step_d", title: "send_email", detail: "失败", state: "done" },
    ]);
    expect(stepsFromTrace([{
      id: "step_plan",
      name: "model",
      status: "succeeded",
      step_index: 1,
      input_json: { status_id: "plan", title: "Planning", detail: "正在判断" },
      output_json: { title: "Planning", detail: "直接回答" },
    }])).toEqual([
      { id: "step_plan", title: "Planning", detail: "直接回答", state: "done" },
    ]);
    expect(stepsFromTrace(null)).toEqual([]);
  });
});

function fact(metric: string, sku: string, value: number, period: string) {
  return {
    fact_type: "sales",
    metric_key: metric,
    numeric_value: value,
    currency: "USD",
    period_start: period,
    sku,
    entity: sku,
    channel: null,
  };
}
