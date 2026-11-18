import { useEffect, useState, type FormEvent } from "react";

import { api, postJson, withUser } from "../api";
import { NeedPartnership, useDesk } from "../desk";
import { shortDate } from "../format";
import { PageHead } from "../ui";

type Memory = {
  id: number;
  kind: string;
  content: string;
  source: string;
  is_active: boolean;
  created_at: string | null;
  last_recalled_at: string | null;
};

type Bundle = { active: Memory[]; inactive: Memory[]; block: string };

export function MemoryPage() {
  return <NeedPartnership><MemoryDesk /></NeedPartnership>;
}

function MemoryDesk() {
  const { partnership, meta, user } = useDesk();
  const p = partnership!;
  const userId = user?.id ?? "";
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [kind, setKind] = useState("preference");
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const kinds = meta?.memory_kinds ?? { preference: "Preference", fact: "Fact", instruction: "Instruction", event: "Event" };

  function load() {
    if (!userId) return Promise.resolve();
    return api<Bundle>(withUser(`/api/partnerships/${p.id}/memories`, userId)).then(setBundle);
  }

  useEffect(() => {
    load().catch((err: Error) => setError(err.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.id, userId]);

  async function add(event: FormEvent) {
    event.preventDefault();
    if (!content.trim()) { setError("Write what to remember first."); return; }
    setError(null);
    try {
      if (!userId) { setError("先在左侧选一个用户。"); return; }
      await postJson(`/api/partnerships/${p.id}/memories`, { kind, content, user_id: userId });
      setContent("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not remember");
    }
  }

  return (
    <>
      <PageHead
        title="Memory"
        caption={`${user ? `${user.name} · ` : ""}${p.brand_name} · ${p.distributor_name} — this person's notes. Colleagues at the same distributor do not see them. Orders, contracts, and reports stay shared.`}
      />
      {error && <div className="error">{error}</div>}
      <form className="card" onSubmit={(event) => void add(event)}>
        <strong>Remember something</strong>
        <div className="filters">
          <label className="field">Kind
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              {Object.entries(kinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          </label>
          <label className="field grow">What to remember
            <input value={content} placeholder="Prefers air freight for launches; their warehouse is closed the first week of July" onChange={(event) => setContent(event.target.value)} />
          </label>
          <button className="btn-primary" type="submit">Remember</button>
        </div>
      </form>
      <h2>Remembered ({bundle?.active.length ?? 0})</h2>
      {bundle && bundle.active.length === 0 && <p className="muted">Nothing yet. Whatever you add here is read into the agent's context on its next turn.</p>}
      {bundle?.active.map((item) => (
        <article className="card" key={item.id}>
          <span className="badge tag">{kinds[item.kind] || item.kind}</span> {item.content}
          <p className="muted">from {item.source} · added {shortDate(item.created_at)} · last used {item.last_recalled_at ? shortDate(item.last_recalled_at) : "never"}</p>
          <div className="actions">
            <button className="btn" type="button" onClick={() => void api(withUser(`/api/memories/${item.id}`, userId), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ active: false }) }).then(load)}>Forget</button>
            <button className="btn" type="button" onClick={() => void api(withUser(`/api/memories/${item.id}`, userId), { method: "DELETE" }).then(load)}>Delete</button>
          </div>
        </article>
      ))}
      {bundle && bundle.inactive.length > 0 && (
        <details className="card">
          <summary>Forgotten ({bundle.inactive.length})</summary>
          {bundle.inactive.map((item) => (
            <div className="row" key={item.id} style={{ justifyContent: "space-between" }}>
              <span><span className="badge mute">{kinds[item.kind] || item.kind}</span> {item.content}</span>
              <span className="actions">
                <button className="btn" type="button" onClick={() => void api(withUser(`/api/memories/${item.id}`, userId), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ active: true }) }).then(load)}>Restore</button>
                <button className="btn" type="button" onClick={() => void api(withUser(`/api/memories/${item.id}`, userId), { method: "DELETE" }).then(load)}>Delete</button>
              </span>
            </div>
          ))}
        </details>
      )}
      <details className="card">
        <summary>What the agent sees</summary>
        <p className="muted">Appended to every agent's prompt on the next turn. Context only: it cannot change a price, relax a rule, or authorise a discount. A chat's rolling summary is separate and stays on that chat.</p>
        <pre>{bundle?.block || "(nothing — no active memories)"}</pre>
      </details>
    </>
  );
}
