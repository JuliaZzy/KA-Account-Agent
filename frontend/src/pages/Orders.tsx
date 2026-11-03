import { useEffect, useState } from "react";

import { api } from "../api";
import { NeedPartnership, useDesk } from "../desk";
import { money, shortDate } from "../format";
import { Badge, DataTable, Metrics, PageHead } from "../ui";

type OrderItem = {
  product_name: string;
  sku: string;
  quantity: number;
  case_price: number;
  line_total: number;
  case_pack: number | null;
};

type OrderEvent = { kind: string; created_at: string | null; data: Record<string, unknown> };

type Order = {
  po_number: string;
  status: string;
  currency: string;
  total_amount: number;
  discount_percentage: number | null;
  discount_amount: number | null;
  subtotal_amount: number;
  items: OrderItem[];
  events: OrderEvent[];
  submitted_at: string | null;
  eta_date: string | null;
  payment_terms: string;
  incoterms: string;
  shipping_method: string;
  ship_to_country: string | null;
  notes: string | null;
  prepaid_amount: number;
  balance_paid: number;
};

export function Orders() {
  return <NeedPartnership><OrdersDesk /></NeedPartnership>;
}

function OrdersDesk() {
  const { partnership } = useDesk();
  const [orders, setOrders] = useState<Order[] | null>(null);
  const [pick, setPick] = useState("");
  const [error, setError] = useState<string | null>(null);
  const p = partnership!;

  useEffect(() => {
    api<Order[]>(`/api/orders?partnership_id=${encodeURIComponent(p.id)}`)
      .then((rows) => {
        setOrders(rows);
        setPick(rows[0]?.po_number ?? "");
      })
      .catch((err: Error) => setError(err.message));
  }, [p.id]);

  const chosen = orders?.find((order) => order.po_number === pick) ?? orders?.[0];
  const cur = orders?.[0]?.currency ?? p.currency;
  const open = (orders ?? []).filter((order) => order.status !== "completed" && order.status !== "cancelled");

  return (
    <>
      <PageHead
        title="Orders"
        caption={`${p.brand_name} · ${p.distributor_name} — placed through the chat, gated by the code-check and a Confirm.`}
      />
      {error && <div className="error">{error}</div>}
      {orders && orders.length === 0 && (
        <div className="empty">No orders yet. Place one in the chat — the draft, the rule check and the Confirm button all live there.</div>
      )}
      {orders && orders.length > 0 && chosen && (
        <>
          <Metrics items={[
            { label: "Orders", value: String(orders.length) },
            { label: "Open", value: String(open.length) },
            { label: "Ordered", value: money(cur, orders.reduce((sum, order) => sum + order.total_amount, 0)) },
            { label: "Cases", value: String(orders.reduce((sum, order) => sum + order.items.reduce((n, item) => n + item.quantity, 0), 0)) },
          ]} />
          <DataTable
            rows={orders}
            columns={[
              { key: "po_number", label: "PO" },
              { key: "status", label: "Status", render: (row) => <Badge status={row.status} /> },
              { key: "total_amount", label: "Total", render: (row) => money(cur, row.total_amount) },
              { key: "discount_percentage", label: "Discount", render: (row) => row.discount_percentage ? `${row.discount_percentage.toFixed(1)}%` : "—" },
              { key: "items", label: "Lines", render: (row) => String(row.items.length) },
              { key: "submitted_at", label: "Submitted", render: (row) => shortDate(row.submitted_at) },
              { key: "eta_date", label: "ETA" },
              { key: "terms", label: "Terms", render: (row) => `${row.payment_terms} · ${row.incoterms} · ${row.shipping_method}` },
            ]}
          />
          <label className="field" style={{ marginTop: 12 }}>Order
            <select value={chosen.po_number} onChange={(event) => setPick(event.target.value)}>
              {orders.map((order) => <option key={order.po_number}>{order.po_number}</option>)}
            </select>
          </label>
          <h2>{chosen.po_number} <Badge status={chosen.status} /></h2>
          <div className="chat-layout" style={{ height: "auto" }}>
            <div>
              <DataTable
                rows={chosen.items}
                columns={[
                  { key: "product_name", label: "Product" },
                  { key: "sku", label: "SKU" },
                  { key: "quantity", label: "Cases" },
                  { key: "case_price", label: "Case price", render: (row) => money(cur, row.case_price) },
                  { key: "line_total", label: "Line total", render: (row) => money(cur, row.line_total) },
                ]}
              />
              <p>
                <strong>Subtotal</strong> {money(cur, chosen.subtotal_amount)} · <strong>Discount</strong>{" "}
                {chosen.discount_percentage ? `${chosen.discount_percentage}%` : chosen.discount_amount ? money(cur, chosen.discount_amount) : "none"}
                {" "}· <strong>Total</strong> {money(cur, chosen.total_amount)}
                <br />
                <strong>Payment</strong> {chosen.payment_terms} · <strong>Incoterms</strong> {chosen.incoterms} · <strong>Shipping</strong> {chosen.shipping_method} · <strong>ETA</strong> {chosen.eta_date || "—"} · <strong>Ship to</strong> {chosen.ship_to_country || "—"}
                {chosen.notes ? <><br /><strong>Notes</strong> {chosen.notes}</> : null}
              </p>
              {(chosen.prepaid_amount || chosen.balance_paid) ? (
                <p className="muted">Prepaid {money(cur, chosen.prepaid_amount)} · balance paid {money(cur, chosen.balance_paid)}</p>
              ) : null}
            </div>
            <div>
              <h3>Timeline</h3>
              <Timeline order={chosen} />
            </div>
          </div>
        </>
      )}
    </>
  );
}

function Timeline({ order }: { order: Order }) {
  const events = [
    ...(order.submitted_at ? [{ kind: "submitted", created_at: order.submitted_at, data: {} }] : []),
    ...order.events,
  ];
  if (!events.length) return <p className="muted">Nothing yet.</p>;
  return (
    <ul className="timeline">
      {events.map((event, index) => {
        const detail = Object.entries(event.data || {})
          .filter(([, value]) => value != null && value !== "" && !(Array.isArray(value) && value.length === 0))
          .map(([key, value]) => `${key} ${String(value)}`)
          .join(", ");
        return <li key={index}><span className="mono">{shortDate(event.created_at)}</span> <strong>{event.kind}</strong>{detail ? ` — ${detail}` : ""}</li>;
      })}
    </ul>
  );
}
