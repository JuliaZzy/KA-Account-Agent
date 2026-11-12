import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import { api } from "./api";
import { chatPresence, forgetChat, isForgotten, useChatPresence } from "./chat-live";
import { chatTitle, tagLabel, type Partnership } from "./format";

export type DeskUser = {
  id: string;
  distributor_id: string;
  name: string;
};

type Named = { id: string; name: string };

function byName(a: Named, b: Named): number {
  return a.name.localeCompare(b.name);
}

function brandsOf(rows: Partnership[]): Named[] {
  const seen = new Map<string, string>();
  for (const row of rows) seen.set(row.brand_id, row.brand_name);
  return [...seen].map(([id, name]) => ({ id, name })).sort(byName);
}

function distributorsOf(rows: Partnership[], brandId: string): Named[] {
  const seen = new Map<string, string>();
  for (const row of rows) {
    if (row.brand_id === brandId) seen.set(row.distributor_id, row.distributor_name);
  }
  return [...seen].map(([id, name]) => ({ id, name })).sort(byName);
}

function resolvePick(
  rows: Partnership[],
  people: DeskUser[],
  brandId: string | null,
  distributorId: string | null,
  userId: string | null,
) {
  const brands = brandsOf(rows);
  const brand = brands.find((item) => item.id === brandId) ?? brands[0] ?? null;
  const distributors = brand ? distributorsOf(rows, brand.id) : [];
  const distributor = distributors.find((item) => item.id === distributorId) ?? distributors[0] ?? null;
  const users = (distributor ? people.filter((item) => item.distributor_id === distributor.id) : []).sort(byName);
  const user = users.find((item) => item.id === userId) ?? users[0] ?? null;
  const partnership = brand && distributor
    ? rows.find((row) => row.brand_id === brand.id && row.distributor_id === distributor.id) ?? null
    : null;
  return { brands, brand, distributors, distributor, users, user, partnership };
}

export type Meta = {
  product_statuses: string[];
  memory_kinds: Record<string, string>;
  max_images: number;
};

export type SessionSummary = {
  id: string;
  title: string | null;
  tags: string[];
  stage: string;
  updated_at: string;
};

type ChatJump = { n: number; sid: string | "new" };

type DeskValue = {
  ready: boolean;
  error: string | null;
  meta: Meta | null;
  partnerships: Partnership[];
  partnership: Partnership | null;
  brands: Named[];
  brand: Named | null;
  distributors: Named[];
  distributor: Named | null;
  users: DeskUser[];
  user: DeskUser | null;
  chooseBrand: (id: string) => void;
  chooseDistributor: (id: string) => void;
  chooseUser: (id: string) => void;
  sessions: SessionSummary[];
  reloadSessions: () => Promise<void>;
  nameSession: (id: string, title: string | null, tags: string[]) => void;
  archiveChat: (id: string) => Promise<void>;
  deleteChat: (id: string) => Promise<void>;
  jump: ChatJump;
  openChat: (sid: string | "new") => void;
};

const DeskContext = createContext<DeskValue | null>(null);

export function useDesk(): DeskValue {
  const value = useContext(DeskContext);
  if (!value) throw new Error("useDesk outside the desk");
  return value;
}

const NAV = [
  { to: "/", label: "Chat", hint: "对话协作", icon: "chat" },
  { to: "/products", label: "Products", hint: "商品目录", icon: "inventory_2" },
  { to: "/orders", label: "Orders", hint: "订单履约", icon: "receipt_long" },
  { to: "/contracts", label: "Contracts", hint: "合同与协议", icon: "description" },
  { to: "/reports", label: "Reports", hint: "数据报告", icon: "analytics" },
  { to: "/memory", label: "Memory", hint: "智能记忆", icon: "neurology" },
  { to: "/monitor", label: "Monitor", hint: "系统监控", icon: "monitoring" },
  { to: "/run-data", label: "Run data", hint: "Run 表数据", icon: "table_chart" },
] as const;

function initials(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  return trimmed.slice(-2);
}

function ago(iso: string): string {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return "Yesterday";
  return `${days}d ago`;
}

const CHAT_MENU = [
  { id: "archive", label: "Archive", hint: "Hide this chat. Reports, contracts, and orders stay.", danger: false },
  { id: "delete", label: "Delete", hint: "Delete this chat and the reports, contracts, and orders it filed.", danger: true },
] as const;

type ChatAction = (typeof CHAT_MENU)[number]["id"];

function ChatMenu({ title, onAction }: { title: string; onAction: (action: ChatAction) => void }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const close = () => setOpen(false);
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    document.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      document.removeEventListener("scroll", close, true);
    };
  }, [open]);

  useLayoutEffect(() => {
    const pop = popRef.current;
    const button = buttonRef.current;
    if (!open || !pop || !button) return;
    const rect = button.getBoundingClientRect();
    const box = pop.getBoundingClientRect();
    let top = rect.bottom + 4;
    if (top + box.height > window.innerHeight - 8) top = Math.max(8, rect.top - 4 - box.height);
    let left = rect.right - box.width;
    if (left < 8) left = 8;
    pop.style.top = `${top}px`;
    pop.style.left = `${left}px`;
  }, [open]);

  return (
    <div className="chat-menu" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className={open ? "chat-menu-btn on" : "chat-menu-btn"}
        aria-label={`Actions for ${title}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="material-symbols-outlined" aria-hidden="true">more_vert</span>
      </button>
      {open && (
        <div className="chat-menu-pop" role="menu" ref={popRef}>
          {CHAT_MENU.map((item) => (
            <button
              key={item.id}
              type="button"
              role="menuitem"
              className={item.danger ? "danger" : undefined}
              title={item.hint}
              onClick={() => {
                setOpen(false);
                onAction(item.id);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function storedPick(): { brandId: string | null; distributorId: string | null; userId: string | null } {
  return {
    brandId: localStorage.getItem("ka_account.brand"),
    distributorId: localStorage.getItem("ka_account.distributor"),
    userId: localStorage.getItem("ka_account.user"),
  };
}

function rememberPick(brandId: string | null, distributorId: string | null, userId: string | null, partnershipId: string | null) {
  const write = (key: string, value: string | null) => {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  };
  write("ka_account.brand", brandId);
  write("ka_account.distributor", distributorId);
  write("ka_account.user", userId);
  write("ka_account.partnership", partnershipId);
}

export function DeskProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [partnerships, setPartnerships] = useState<Partnership[]>([]);
  const [people, setPeople] = useState<DeskUser[]>([]);
  const [brandId, setBrandId] = useState<string | null>(null);
  const [distributorId, setDistributorId] = useState<string | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [jump, setJump] = useState<ChatJump>({ n: 0, sid: "" });
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    Promise.all([api<Meta>("/api/meta"), api<Partnership[]>("/api/partnerships"), api<DeskUser[]>("/api/users")])
      .then(([nextMeta, nextPartnerships, nextUsers]) => {
        const saved = storedPick();
        const previous = nextPartnerships.find((row) => row.id === localStorage.getItem("ka_account.partnership"));
        const pick = resolvePick(
          nextPartnerships,
          nextUsers,
          saved.brandId ?? previous?.brand_id ?? null,
          saved.distributorId ?? previous?.distributor_id ?? null,
          saved.userId,
        );
        setMeta(nextMeta);
        setPartnerships(nextPartnerships);
        setPeople(nextUsers);
        setBrandId(pick.brand?.id ?? null);
        setDistributorId(pick.distributor?.id ?? null);
        setUserId(pick.user?.id ?? null);
        rememberPick(pick.brand?.id ?? null, pick.distributor?.id ?? null, pick.user?.id ?? null, pick.partnership?.id ?? null);
        setError(null);
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setReady(true));
  }, []);

  const pick = useMemo(
    () => resolvePick(partnerships, people, brandId, distributorId, userId),
    [partnerships, people, brandId, distributorId, userId],
  );
  const partnership = pick.partnership;

  const applyPick = useCallback((nextBrand: string | null, nextDistributor: string | null, nextUser: string | null) => {
    const next = resolvePick(partnerships, people, nextBrand, nextDistributor, nextUser);
    setBrandId(next.brand?.id ?? null);
    setDistributorId(next.distributor?.id ?? null);
    setUserId(next.user?.id ?? null);
    rememberPick(next.brand?.id ?? null, next.distributor?.id ?? null, next.user?.id ?? null, next.partnership?.id ?? null);
    setJump({ n: 0, sid: "" });
  }, [partnerships, people]);

  // A slow list from an earlier turn must not paint over a chat that was created since.
  const reloadGen = useRef(0);
  const reloadSessions = useCallback(async () => {
    const pid = partnership?.id;
    if (!pid) {
      setSessions([]);
      return;
    }
    const gen = ++reloadGen.current;
    const params = new URLSearchParams({ partnership_id: pid });
    if (pick.user) params.set("user_id", pick.user.id);
    const rows = await api<SessionSummary[]>(`/api/sessions?${params}`);
    if (gen !== reloadGen.current) return;
    setSessions(rows.filter((row) => !isForgotten(row.id)));
  }, [partnership?.id, pick.user]);

  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const closing = useRef(new Set<string>());
  const dropChat = useCallback((id: string) => {
    const wasOpen = chatPresence().activeId === id;
    forgetChat(id);
    const rest = sessionsRef.current.filter((session) => session.id !== id);
    sessionsRef.current = rest;
    setSessions(rest);
    if (wasOpen) {
      setJump((current) => ({ n: current.n + 1, sid: rest[0]?.id ?? "new" }));
      navigate("/");
    }
  }, [navigate]);
  const closeChat = useCallback(async (id: string, path: string, method: string, failure: string) => {
    if (closing.current.has(id)) return;
    closing.current.add(id);
    try {
      await api<void>(path, { method });
      dropChat(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : failure);
    } finally {
      closing.current.delete(id);
    }
  }, [dropChat]);
  const archiveChat = useCallback(
    (id: string) => closeChat(id, `/api/sessions/${id}/archive`, "POST", "Could not archive the chat"),
    [closeChat],
  );
  const deleteChat = useCallback(
    (id: string) => closeChat(id, `/api/sessions/${id}`, "DELETE", "Could not delete the chat"),
    [closeChat],
  );

  const nameSession = useCallback((id: string, title: string | null, tags: string[]) => {
    setSessions((current) => current.map((session) => (
      session.id === id ? { ...session, title: title ?? session.title, tags } : session
    )));
  }, []);

  useEffect(() => {
    reloadSessions().catch((err: Error) => setError(err.message));
  }, [reloadSessions]);

  const value = useMemo<DeskValue>(() => ({
    ready,
    error,
    meta,
    partnerships,
    partnership,
    brands: pick.brands,
    brand: pick.brand,
    distributors: pick.distributors,
    distributor: pick.distributor,
    users: pick.users,
    user: pick.user,
    chooseBrand: (id: string) => applyPick(id, distributorId, userId),
    chooseDistributor: (id: string) => applyPick(brandId, id, userId),
    chooseUser: (id: string) => applyPick(brandId, distributorId, id),
    sessions,
    reloadSessions,
    nameSession,
    archiveChat,
    deleteChat,
    jump,
    openChat: (sid: string | "new") => {
      setJump((current) => ({ n: current.n + 1, sid }));
      navigate("/");
    },
  }), [
    ready, error, meta, partnerships, partnership, pick, applyPick, brandId, distributorId, userId,
    sessions, reloadSessions, nameSession, archiveChat, deleteChat, jump, navigate,
  ]);

  return <DeskContext.Provider value={value}>{children}</DeskContext.Provider>;
}

export function Shell({ children }: { children: ReactNode }) {
  const desk = useDesk();
  const presence = useChatPresence();
  const { pathname } = useLocation();
  const page = NAV.find((item) => item.to === pathname);
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-scroll">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">KA</span>
            <strong>KA Account</strong>
          </div>
          {desk.brands.length > 0 && (
            <div className="account-card">
              <div className="account-kicker">Account</div>
              <label className="account-brand">
                <span className="sr-only">Brand</span>
                <select aria-label="Brand" value={desk.brand?.id ?? ""} onChange={(event) => desk.chooseBrand(event.target.value)}>
                  {desk.brands.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select>
              </label>
              <label className="account-line">
                <span className="sr-only">Distributor</span>
                <select
                  aria-label="Distributor"
                  value={desk.distributor?.id ?? ""}
                  onChange={(event) => desk.chooseDistributor(event.target.value)}
                >
                  {desk.distributors.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select>
              </label>
              <label className="account-line">
                <span className="sr-only">User</span>
                <select aria-label="User" value={desk.user?.id ?? ""} onChange={(event) => desk.chooseUser(event.target.value)}>
                  {desk.users.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select>
              </label>
            </div>
          )}
          <button className="btn-primary" type="button" onClick={() => desk.openChat("new")}>
            <span className="material-symbols-outlined" aria-hidden="true">add</span>
            新建对话
          </button>
          <nav className="nav">
            {NAV.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.to === "/"} className={({ isActive }) => (isActive ? "on" : "")}>
                <span className="material-symbols-outlined" aria-hidden="true">{item.icon}</span>
                <span className="nav-copy">{item.label} <span className="nav-hint">{item.hint}</span></span>
              </NavLink>
            ))}
          </nav>
          <div className="side-label">
            <span>Recent Sessions</span>
            <span className="material-symbols-outlined" aria-hidden="true">history</span>
          </div>
          <div className="chat-list">
            {desk.sessions.map((session) => {
              const replying = presence.busyIds.includes(session.id);
              const title = chatTitle(session);
              const when = ago(session.updated_at);
              return (
                <div key={session.id} className={presence.activeId === session.id ? "chat-row on" : "chat-row"}>
                  <button type="button" className="chat-open" onClick={() => desk.openChat(session.id)}>
                    <span className="chat-name">{title}</span>
                    <span className="chat-meta">
                      {replying && <small className="replying">Replying…</small>}
                      {tagLabel(session.tags) && <small className="chat-tag">{tagLabel(session.tags)}</small>}
                      {when && <small className="chat-when">{when}</small>}
                    </span>
                  </button>
                  <ChatMenu
                    title={title}
                    onAction={(action) => {
                      if (action === "archive") void desk.archiveChat(session.id);
                      else void desk.deleteChat(session.id);
                    }}
                  />
                </div>
              );
            })}
          </div>
        </div>
        {desk.user && (
          <div className="sidebar-foot">
            <div className="avatar user" aria-hidden="true">{initials(desk.user.name)}</div>
            <div className="foot-copy">
              <div className="foot-name">{desk.user.name}</div>
              {desk.distributor && <div className="foot-sub">{desk.distributor.name}</div>}
            </div>
          </div>
        )}
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="crumbs">
            {desk.brand ? <strong>{desk.brand.name}</strong> : <strong>KA Account</strong>}
            {desk.distributor && <><span>·</span><span>{desk.distributor.name}</span></>}
            {desk.user && <><span>·</span><span>{desk.user.name}</span></>}
            {desk.partnership?.ship_to_country && <span className="chip">{desk.partnership.ship_to_country}</span>}
          </div>
          <div className="topbar-end">
            {page && <span className="page-chip">{page.label}</span>}
            {desk.user && <div className="avatar user sm" aria-hidden="true">{initials(desk.user.name)}</div>}
          </div>
        </header>
        <main className={pathname === "/" ? "main main-chat" : "main"}>
          {desk.error && <div className="error">{desk.error}</div>}
          {children}
        </main>
      </div>
    </div>
  );
}

export function NeedPartnership({ children }: { children: ReactNode }) {
  const { ready, partnership } = useDesk();
  if (!ready) return <p className="muted">Opening…</p>;
  if (!partnership) {
    return (
      <div className="empty">
        No brand–distributor partnership is set up yet, so there is nothing to order or report on.
        The product catalog is under Products.
      </div>
    );
  }
  return children;
}
