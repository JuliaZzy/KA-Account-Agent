import { useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";

import { api, cancelRun, followRun, getRunEvents, postJson, resumeRun, streamMessage, type RunEvent } from "../api";
import { CHAT_DOC_EXTS, CHAT_IMAGE_EXTS, clipAttachedFiles, fileExt } from "../chat-files";
import { flightOf, isChatBusy, isForgotten, onForgetChat, patchFlight, setActiveChat, useFlight } from "../chat-live";
import { NeedPartnership, useDesk } from "../desk";
import {
  citationSources,
  contractFiling,
  dataQueryMark,
  discountText,
  greeting,
  lineFlag,
  money,
  activitiesFromEvents,
  trimNum,
  reportCharts,
  resultLink,
  resultPanes,
  webNotes,
  type Activity,
  type ContractFiling,
  type DataQueryMark,
  type DraftLine,
  type ReportChart,
} from "../format";
import { Markdown } from "../markdown-view";
import { DataTable, Tabs } from "../ui";

type Turn = {
  role: "user" | "assistant";
  text: string;
  images?: string[];
  files?: string[];
  model?: string;
  warnings?: string[];
  sources?: { n: number; source: string }[];
  dataQuery?: DataQueryMark;
  charts?: ReportChart[];
  webNotes?: string[];
  reportDraft?: boolean;
  reportBody?: string;
  reportPendingId?: string;
  reportId?: string;
  reportTitle?: string;
  contract?: ContractFiling;
  order?: boolean;
  combined?: boolean;
  /** This reply's chat run. A reload reads it and fetches the trace. */
  runId?: string;
  activity?: Activity[];
};

type Gap = { message: string; candidates?: { name: string; sku: string }[] };

type Draft = {
  currency: string;
  lines: DraftLine[];
  payment_terms: string;
  incoterms: string;
  shipping_method: string;
  eta_date: string | null;
  discount: { kind: string; value?: number | null; source?: string | null } | null;
  notes?: string | null;
  total: number | null;
};

type Snap = {
  turns: Turn[];
  draft: Draft | null;
  gaps: Gap[];
  stage: string;
  confirmToken: string | null;
  route: string;
  title: string | null;
  tags: string[];
};

type SessionRow = {
  id: string;
  stage?: string;
  title?: string | null;
  tags?: string[];
  confirm_token?: string | null;
  history?: {
    role: string;
    content?: string;
    sources?: { n: number; source: string }[];
    data_query?: unknown;
    charts?: unknown;
    web_notes?: unknown;
    report_draft?: boolean;
    report_pending_id?: string;
    report_id?: string;
    report_title?: string;
    contract?: unknown;
    order?: boolean;
    order_draft?: unknown;
    report_body?: string;
    combined?: boolean;
    run_id?: string;
  }[];
};

const snaps = new Map<string, Snap>();
onForgetChat((id) => snaps.delete(id));
const SKIP = new Set(["(message)", "(confirm)"]);

function waitingToolName(events: RunEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    if (events[i].event_type !== "run.waiting_for_user") continue;
    const name = events[i].payload_json?.tool_name;
    if (typeof name === "string" && name) return name;
  }
  return null;
}

function paneIndex(id: string, prefix: string): number | null {
  if (!id.startsWith(prefix)) return null;
  const n = Number(id.slice(prefix.length));
  return Number.isInteger(n) ? n : null;
}

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function orderDraft(raw: unknown): Draft | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const row = raw as Draft;
  return Array.isArray(row.lines) ? row : null;
}

function fromSession(session: SessionRow, hello: string): Snap {
  const turns: Turn[] = [{ role: "assistant", text: hello }];
  let draft: Draft | null = null;
  for (const message of session.history ?? []) {
    const text = String(message.content ?? "");
    if ((message.role === "user" || message.role === "assistant") && !SKIP.has(text)) {
      const sources = message.role === "assistant" ? citationSources(message.sources) : undefined;
      const dataQuery = message.role === "assistant" ? dataQueryMark(message.data_query) : undefined;
      const charts = message.role === "assistant" ? reportCharts(message.charts) : undefined;
      const notes = message.role === "assistant" ? webNotes(message.web_notes) : undefined;
      const reportDraft = message.role === "assistant" && message.report_draft === true;
      const reportBody = message.role === "assistant" && message.report_body ? message.report_body : undefined;
      const reportPendingId = message.role === "assistant" && message.report_pending_id ? message.report_pending_id : undefined;
      const reportId = message.role === "assistant" && message.report_id ? message.report_id : undefined;
      const reportTitle = message.role === "assistant" && message.report_title
        ? message.report_title.trim() || undefined
        : undefined;
      const contract = message.role === "assistant" ? contractFiling(message.contract) : undefined;
      const order = message.role === "assistant" && message.order === true;
      const combined = message.role === "assistant" && message.combined === true;
      const runId = message.role === "assistant" && message.run_id ? message.run_id : undefined;
      const savedDraft = orderDraft(message.order_draft);
      if (savedDraft) draft = savedDraft;
      turns.push({
        role: message.role, text, sources, dataQuery, charts, webNotes: notes,
        reportDraft, reportBody, reportPendingId, reportId, reportTitle, contract, order, combined, runId,
      });
    }
  }
  const stage = session.stage || "gathering";
  return {
    turns,
    draft,
    gaps: [],
    stage,
    confirmToken: session.confirm_token ?? null,
    route: "",
    title: session.title ?? null,
    tags: session.tags ?? [],
  };
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <circle cx="12" cy="12" r="8.25" fill="none" stroke="currentColor" strokeWidth="1.75" />
      <rect x="8.25" y="8.25" width="7.5" height="7.5" fill="currentColor" />
    </svg>
  );
}

export function Chat() {
  return (
    <NeedPartnership>
      <ChatDesk />
    </NeedPartnership>
  );
}

function ChatDesk() {
  const desk = useDesk();
  const partnership = desk.partnership!;
  const userId = desk.user?.id ?? "";
  const [sid, setSid] = useState<string | null>(null);
  const [snap, setSnap] = useState<Snap | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [attachNote, setAttachNote] = useState<string | null>(null);
  const [model, setModel] = useState(() => localStorage.getItem("ka_account.model") || "");
  const threadRef = useRef<HTMLDivElement>(null);
  const bootKey = useRef("");
  const sidRef = useRef<string | null>(null);
  const openSeq = useRef(0);
  // True while an IME preedit is still uncommitted. Enter confirms that
  // preedit; it must not also send, or the commit lands in the cleared box.
  const composing = useRef(false);
  const stops = useRef(new Map<string, AbortController>());
  const flight = useFlight(sid);
  const [traceSteps, setTraceSteps] = useState<Activity[]>([]);

useEffect(() => {
  setTraceSteps([]);
}, [flight.runId]);
  sidRef.current = sid;
  const pendingKinds = flight.tasks;
  const liveKind = pendingKinds.length > 1
    ? null
    : flight.task === "report" || flight.task === "contract" || flight.task === "po" ? flight.task : null;
  const liveTask = liveKind ? { kind: liveKind, text: flight.live } : null;
  const panes = resultPanes({
    hasOrder: Boolean(snap?.draft),
    reports: (snap?.turns ?? []).flatMap((turn, index) => (
      turn.reportDraft ? [{ id: `report-${index}`, text: turn.reportBody || turn.text, charts: turn.charts }] : []
    )),
    contracts: (snap?.turns ?? []).flatMap((turn, index) => (
      turn.contract ? [{ id: `contract-${index}`, filing: turn.contract }] : []
    )),
    live: liveTask,
    pending: pendingKinds.length > 1 ? pendingKinds : [],
  });
  const paneKey = panes.map((pane) => pane.id).join("|");
  const [picked, setPicked] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const panelWidthRef = useRef(480);
  const [panelWidth, setPanelWidth] = useState(() => {
    const saved = Number(localStorage.getItem("ka_account.panelWidth"));
    const width = Number.isFinite(saved) && saved >= 300 ? Math.round(saved) : 480;
    panelWidthRef.current = width;
    return width;
  });
  const seenPanes = useRef("");
  const seenChat = useRef<string | null>(null);
  useEffect(() => {
    if (seenChat.current !== sid) {
      seenChat.current = sid;
      seenPanes.current = "";
      setPanelOpen(true);
    }
    const ids = paneKey ? paneKey.split("|") : [];
    const previous = seenPanes.current ? seenPanes.current.split("|") : [];
    const added = ids.filter((id) => !previous.includes(id));
    seenPanes.current = paneKey;
    if (added.length) {
      setPicked(added[added.length - 1]);
      setPanelOpen(true);
    } else setPicked((current) => (current && ids.includes(current) ? current : ids.at(-1) ?? null));
  }, [paneKey, sid]);
  const activePane = panes.find((pane) => pane.id === picked) ?? panes.at(-1) ?? null;

  function openResult(id: string) {
    setPicked(id);
    setPanelOpen(true);
  }

  function focus(nextSid: string, next: Snap) {
    if (isForgotten(nextSid)) return;
    snaps.set(nextSid, next);
    sidRef.current = nextSid;
    setSid(nextSid);
    setSnap(next);
    setActiveChat(nextSid);
  }

  function commitSnap(id: string, next: Snap) {
    if (isForgotten(id)) return;
    snaps.set(id, next);
    if (sidRef.current === id) setSnap(next);
  }

  async function withSavedTraces(turns: Turn[]): Promise<Turn[]> {
    if (!userId) return turns;
    const orgId = partnership.id;
    return Promise.all(turns.map(async (turn) => {
      if (turn.role !== "assistant" || !turn.runId) return turn;
      try {
        const events = await getRunEvents(turn.runId, orgId, userId);
        const steps = activitiesFromEvents(events);
        return steps.length ? { ...turn, activity: steps } : turn;
      } catch {
        return turn;
      }
    }));
  }

  async function openExisting(id: string, seq: number) {
    const cached = snaps.get(id);
    if (cached) {
      if (seq === openSeq.current) focus(id, cached);
      return;
    }
    const session = await api<SessionRow>(`/api/sessions/${id}`);
    if (seq !== openSeq.current) return;
    if (snaps.has(id)) {
      focus(id, snaps.get(id)!);
      return;
    }
    const next = fromSession(session, greeting(partnership));
    const turns = await withSavedTraces(next.turns);
    if (seq !== openSeq.current) return;
    focus(id, { ...next, turns });
  }

  async function startNew(seq: number) {
    const session = await postJson<SessionRow>("/api/sessions", {
      partnership_id: partnership.id,
      ...(userId ? { user_id: userId } : {}),
    });
    if (seq !== openSeq.current) return;
    const fresh: Snap = {
      turns: [{ role: "assistant", text: greeting(partnership) }],
      draft: null, gaps: [], stage: "gathering", confirmToken: null, route: "", title: null, tags: [],
    };
    focus(session.id, fresh);
    await desk.reloadSessions();
  }

  useEffect(() => onForgetChat((id) => {
    if (sidRef.current !== id) return;
    sidRef.current = null;
    setSid(null);
    setSnap(null);
  }), []);

  useEffect(() => {
    const jump = desk.jump;
    const key = `${partnership.id}:${userId}:${jump.n}:${jump.sid}`;
    if (bootKey.current === key) return;
    bootKey.current = key;
    const seq = ++openSeq.current;
    setBootError(null);
    const run = async () => {
      if (jump.n > 0 && jump.sid === "new") {
        // Paint the switch before the create request. A reply still holding its
        // stream used to leave this click with nothing on screen until that request returned.
        sidRef.current = null;
        setSid(null);
        setActiveChat(null);
        setSnap({
          turns: [{ role: "assistant", text: "Opening…" }],
          draft: null, gaps: [], stage: "gathering", confirmToken: null, route: "", title: null, tags: [],
        });
        await startNew(seq);
        return;
      }
      if (jump.n > 0 && jump.sid) {
        await openExisting(jump.sid, seq);
        return;
      }
      const params = new URLSearchParams({ partnership_id: partnership.id });
      if (userId) params.set("user_id", userId);
      const rows = await api<{ id: string }[]>(`/api/sessions?${params}`);
      if (seq !== openSeq.current) return;
      if (rows[0]) await openExisting(rows[0].id, seq);
      else await startNew(seq);
    };
    run().catch((err: Error) => {
      if (seq === openSeq.current) setBootError(err.message);
    });
    // startNew/openExisting close over the current partnership; jump is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [partnership.id, userId, desk.jump]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [snap?.turns, flight.live, traceSteps]);

  const models = desk.meta?.models ?? [];
  const listed = models.some((item) => item.id === model);
  const modelId = (listed ? model : "") || desk.meta?.default_model || models[0]?.id || "";

  async function send(message: string, attached?: File[]) {
    const chatId = sidRef.current;
    const baseSnap = chatId ? snaps.get(chatId) : undefined;
    if (!chatId || !baseSnap || isChatBusy(chatId)) return;
    const chosen = attached ?? flightOf(chatId).files;
    const trimmed = message.trim();
    if (!trimmed && chosen.length === 0) return;
    const stop = new AbortController();
    stops.current.set(chatId, stop);
    setAttachNote(null);
    patchFlight(chatId, {
      busy: true, streaming: true, runId: null, error: null, approvalTool: null,
      draft: "", files: [], live: "", task: null, tasks: [],
    });
    const maxImages = desk.meta?.max_images;
    const images: string[] = [];
    const documents: { name: string; data: string }[] = [];
    for (const file of chosen) {
      const ext = fileExt(file.name);
      if (CHAT_IMAGE_EXTS.has(ext)) {
        if (maxImages != null && images.length < maxImages) images.push(await readDataUrl(file));
      } else if (CHAT_DOC_EXTS.has(ext) && documents.length < 2) {
        documents.push({ name: file.name, data: await readDataUrl(file) });
      }
    }
    const userTurn: Turn = { role: "user", text: trimmed, images, files: documents.map((d) => d.name) };
    const base: Snap = { ...baseSnap, turns: [...baseSnap.turns, userTurn] };
    commitSnap(chatId, base);
    let steps: Activity[] = [];
    let title = base.title;
    let tags = base.tags;
    let committed = false;
    let parkedTool: string | null = null;
    try {
      await streamMessage(chatId, {
        message: trimmed,
        images,
        attachments: documents.map((d) => d.name),
        files: documents,
        model: modelId || null,
      }, async (event) => {
        if (typeof event.run_id === "string" && event.run_id) patchFlight(chatId, { runId: event.run_id });
        if (event.type === "session") {
          title = (event.title as string | null) ?? title;
          tags = (event.tags as string[]) ?? tags;
          desk.nameSession(chatId, title, tags);
        } else if (event.type === "error") {
          throw new Error(String(event.message ?? "The model could not finish this reply."));
        }
      }, stop.signal);
      const runId = flightOf(chatId).runId;
      if (!runId || !userId) throw new Error("run was not created");
      const ended = await followRun(runId, partnership.id, userId, stop.signal, (events, run) => {
        steps = activitiesFromEvents(events, run.status);
        setTraceSteps(steps);
        const partial = run.output_json?.text;
        if (typeof partial === "string" && partial) patchFlight(chatId, { live: partial });
        parkedTool = waitingToolName(events) ?? parkedTool;
      });
      if (ended.status === "cancelled") {
        committed = true;
        return;
      }
      if (ended.status === "failed") {
        throw new Error(ended.error_json?.message || "The model could not finish this reply.");
      }
      if (ended.status === "waiting_for_user") {
        committed = true;
        patchFlight(chatId, { approvalTool: parkedTool ?? "这个工具" });
        return;
      }
      const session = await api<SessionRow>(`/api/sessions/${chatId}`);
      const next = fromSession(session, greeting(partnership));
      const last = next.turns[next.turns.length - 1];
      if (last?.role === "assistant" && steps.length) last.activity = steps;
      commitSnap(chatId, { ...next, title: next.title ?? title, tags: next.tags.length ? next.tags : tags });
      committed = true;
    } catch (err) {
      // Abort is the cancel click, or leaving the page. Neither is a send failure.
      if (!committed && !(err instanceof Error && err.name === "AbortError")) {
        patchFlight(chatId, { error: err instanceof Error ? err.message : "send failed" });
      }
    } finally {
      stops.current.delete(chatId);
      patchFlight(chatId, { busy: false, streaming: false, live: "", task: null, tasks: [] });
    }
    void desk.reloadSessions().catch(() => undefined);
  }

  async function resumeApproval(decision: "confirm" | "skip") {
    const chatId = sidRef.current;
    if (!chatId || !userId) return;
    const current = flightOf(chatId);
    const runId = current.runId;
    if (!runId || current.busy) return;
    const stop = new AbortController();
    stops.current.set(chatId, stop);
    patchFlight(chatId, { busy: true, streaming: true, error: null });
    let steps: Activity[] = traceSteps;
    let parkedTool = current.approvalTool;
    let committed = false;
    try {
      await resumeRun(runId, partnership.id, userId, {
        message: decision === "skip" ? "跳过" : "确认",
        decision,
      });
      const ended = await followRun(runId, partnership.id, userId, stop.signal, (events, run) => {
        steps = activitiesFromEvents(events, run.status);
        setTraceSteps(steps);
        const partial = run.output_json?.text;
        if (typeof partial === "string" && partial) patchFlight(chatId, { live: partial });
        parkedTool = waitingToolName(events) ?? parkedTool;
      });
      if (ended.status === "cancelled") {
        committed = true;
        patchFlight(chatId, { approvalTool: null });
        return;
      }
      if (ended.status === "failed") {
        throw new Error(ended.error_json?.message || "The model could not finish this reply.");
      }
      if (ended.status === "waiting_for_user") {
        committed = true;
        patchFlight(chatId, { approvalTool: parkedTool ?? "这个工具" });
        return;
      }
      const session = await api<SessionRow>(`/api/sessions/${chatId}`);
      const next = fromSession(session, greeting(partnership));
      const last = next.turns[next.turns.length - 1];
      if (last?.role === "assistant" && steps.length) last.activity = steps;
      const base = snaps.get(chatId);
      commitSnap(chatId, {
        ...next,
        title: next.title ?? base?.title ?? null,
        tags: next.tags.length ? next.tags : (base?.tags ?? []),
      });
      committed = true;
      patchFlight(chatId, { approvalTool: null });
    } catch (err) {
      if (!committed && !(err instanceof Error && err.name === "AbortError")) {
        patchFlight(chatId, { error: err instanceof Error ? err.message : "send failed" });
      }
    } finally {
      stops.current.delete(chatId);
      patchFlight(chatId, { busy: false, streaming: false, live: "", task: null, tasks: [] });
    }
    void desk.reloadSessions().catch(() => undefined);
  }

  async function cancelTurn() {
    const chatId = sidRef.current;
    if (!chatId) return;
    const runId = flightOf(chatId).runId;
    if (runId && userId) {
      try {
        await cancelRun(runId, partnership.id, userId);
      } catch (err) {
        patchFlight(chatId, { error: err instanceof Error ? err.message : "cancel failed" });
      }
    }
    stops.current.get(chatId)?.abort();
  }

  async function confirm() {
    const chatId = sidRef.current;
    const current = chatId ? snaps.get(chatId) : undefined;
    if (!chatId || !current?.confirmToken || isChatBusy(chatId)) return;
    patchFlight(chatId, { busy: true, error: null });
    try {
      const result = await postJson<{ state: string; message?: string; draft?: Draft | null }>(
        `/api/sessions/${chatId}/confirm`,
        { token: current.confirmToken },
      );
      const textOut = result.state === "submitted" || result.state === "changed"
        ? (result.message ?? "")
        : `Confirm: ${result.state}.`;
      const next: Snap = {
        ...current,
        turns: [...current.turns, { role: "assistant", text: textOut }],
        stage: result.state === "submitted" ? "submitted" : result.state === "changed" ? "gathering" : current.stage,
        draft: result.state === "submitted" || result.state === "changed" ? (result.draft ?? null) : current.draft,
        gaps: result.state === "submitted" || result.state === "changed" ? [] : current.gaps,
        confirmToken: null,
      };
      commitSnap(chatId, next);
      await desk.reloadSessions();
    } catch (err) {
      patchFlight(chatId, { error: err instanceof Error ? err.message : "confirm failed" });
    } finally {
      patchFlight(chatId, { busy: false });
    }
  }

  function updateTurn(index: number, patch: Partial<Turn>) {
    const chatId = sidRef.current;
    const current = chatId ? snaps.get(chatId) : undefined;
    if (!chatId || !current || !current.turns[index]) return;
    commitSnap(chatId, {
      ...current,
      turns: current.turns.map((turn, i) => (i === index ? { ...turn, ...patch } : turn)),
    });
  }

  async function confirmContract(filing: ContractFiling) {
    const chatId = sidRef.current;
    const current = chatId ? snaps.get(chatId) : undefined;
    if (!chatId || !current || !filing.pending_id || isChatBusy(chatId)) return;
    patchFlight(chatId, { busy: true, error: null });
    try {
      const result = await postJson<{ contract_id: string; contract: ContractFiling; message: string }>(
        `/api/sessions/${chatId}/confirm-contract`,
        { pending_id: filing.pending_id, filing },
      );
      const saved = contractFiling(result.contract) ?? filing;
      const latest = snaps.get(chatId) ?? current;
      commitSnap(chatId, {
        ...latest,
        turns: [
          ...latest.turns.map((turn) => (
            turn.contract?.pending_id === filing.pending_id ? { ...turn, contract: saved } : turn
          )),
          { role: "assistant", text: result.message },
        ],
      });
    } catch (err) {
      patchFlight(chatId, { error: err instanceof Error ? err.message : "confirm failed" });
    } finally {
      patchFlight(chatId, { busy: false });
    }
  }

  async function confirmReport(index: number) {
    const chatId = sidRef.current;
    const current = chatId ? snaps.get(chatId) : undefined;
    const turn = current?.turns[index];
    if (!chatId || !current || !turn?.reportPendingId || isChatBusy(chatId)) return;
    const text = turn.reportBody || turn.text;
    const title = turn.reportTitle || "";
    const charts = (turn.charts ?? []).map((chart) => ({
      id: chart.id, title: chart.title, subtitle: chart.subtitle, echarts_option: chart.option,
    }));
    patchFlight(chatId, { busy: true, error: null });
    try {
      const result = await postJson<{ id: string; title: string; message: string }>(
        `/api/sessions/${chatId}/confirm-report`,
        { pending_id: turn.reportPendingId, title, text, charts },
      );
      const latest = snaps.get(chatId) ?? current;
      commitSnap(chatId, {
        ...latest,
        turns: [
          ...latest.turns.map((item, i) => (
            i === index ? { ...item, text, reportTitle: result.title, reportId: result.id } : item
          )),
          { role: "assistant", text: result.message },
        ],
      });
    } catch (err) {
      patchFlight(chatId, { error: err instanceof Error ? err.message : "confirm failed" });
    } finally {
      patchFlight(chatId, { busy: false });
    }
  }

  function markFeedback(logId: number, feedback: "good" | "bad") {
    const chatId = sidRef.current;
    const current = chatId ? snaps.get(chatId) : undefined;
    if (!chatId || !current) return;
    commitSnap(chatId, {
      ...current,
      turns: current.turns.map((turn) => (
        turn.dataQuery?.logId === logId ? { ...turn, dataQuery: { ...turn.dataQuery, feedback } } : turn
      )),
    });
  }

  const showPanel = panelOpen && panes.length > 0;
  const livePane = liveKind === "report"
    ? panes.find((pane) => pane.id === "report-live")
    : liveKind === "contract"
      ? panes.find((pane) => pane.id === "contract-live")
      : liveKind === "po"
        ? panes.find((pane) => pane.kind === "order")
        : undefined;
  const artifactIndex = activePane?.kind === "report" || activePane?.kind === "contract"
    ? paneIndex(activePane.id, activePane.kind === "report" ? "report-" : "contract-")
    : null;
  const artifactTurn = artifactIndex != null ? snap?.turns[artifactIndex] : undefined;
  const reportNotes = activePane?.kind === "report" ? artifactTurn?.webNotes : undefined;
  const reportTitle = artifactTurn?.reportTitle || "";
  const layoutStyle: CSSProperties | undefined = showPanel
    ? { "--result-width": `${panelWidth}px` } as CSSProperties
    : undefined;

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const layout = event.currentTarget.parentElement;
    if (!layout) return;
    const startX = event.clientX;
    const startW = panelWidthRef.current;
    const max = Math.max(300, Math.round(layout.clientWidth * 0.72));
    const move = (ev: PointerEvent) => {
      const next = Math.round(Math.min(max, Math.max(300, startW + startX - ev.clientX)));
      panelWidthRef.current = next;
      setPanelWidth(next);
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      localStorage.setItem("ka_account.panelWidth", String(panelWidthRef.current));
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }

  return (
    <div className={`chat-layout${showPanel ? " with-result" : ""}`} style={layoutStyle}>
      <section className="thread-col">
        <header className="page-head">
          <h1>{partnership.brand_name} · {partnership.distributor_name}</h1>
          <p>
            {desk.user ? `${desk.user.name} · ` : ""}
            ships to {partnership.ship_to_country || "—"}
            {snap?.route && <span className="route">{snap.route}</span>}
          </p>
        </header>
        {(bootError || flight.error) && <div className="error">{bootError || flight.error}</div>}
        <div className="thread" ref={threadRef}>
          {snap?.turns.map((turn, index) => {
            const links = [
              turn.order ? panes.find((item) => item.kind === "order" && !item.pending) : undefined,
              turn.reportDraft ? panes.find((item) => item.id === `report-${index}`) : undefined,
              turn.contract ? panes.find((item) => item.id === `contract-${index}`) : undefined,
            ].flatMap((item) => (item ? [item] : []));
            const showAnswer = Boolean(turn.combined || turn.reportBody || links.length > 1);
            const artifact = links.length > 0 && !showAnswer;
            return (
            <div className={`turn ${turn.role}`} key={index}>
              <div className="bubble">
                {turn.activity && turn.activity.length > 0 && (
                  <ActivityList steps={turn.activity} />
                )}
                {turn.model && <div className="who">{models.find((item) => item.id === turn.model)?.short || turn.model}</div>}
                {showAnswer && (
                  turn.role === "assistant"
                    ? <Markdown text={turn.text} charts={turn.reportBody ? undefined : turn.charts} />
                    : <p>{turn.text}</p>
                )}
                {showAnswer && links.map((pane) => (
                  <button type="button" className="result-link" key={pane.id} onClick={() => openResult(pane.id)}>{resultLink(pane.label)}</button>
                ))}
                {!showAnswer && (artifact
                  ? <button type="button" className="result-link" onClick={() => openResult(links[0].id)}>{resultLink(links[0].label)}</button>
                  : turn.role === "assistant"
                    ? <Markdown text={turn.text} charts={turn.charts} />
                    : <p>{turn.text}</p>)}
                {!artifact && !showAnswer && turn.webNotes && turn.webNotes.length > 0 && (
                  <div className="refs">
                    <div className="refs-label">参考资料：</div>
                    {turn.webNotes.map((note, noteIndex) => (
                      <div className="source" key={`${noteIndex}-${note}`}>[{noteIndex + 1}] {note}</div>
                    ))}
                  </div>
                )}
                {turn.files?.map((name) => <div className="who" key={name}>📎 {name}</div>)}
                {turn.images?.map((src) => <img key={src.slice(0, 32)} alt="" src={src} />)}
                {turn.sources?.map((source) => (
                  <div className="source" key={source.n}>[{source.n}] {source.source}</div>
                ))}
                {turn.warnings?.map((warning) => <div className="warn" key={warning}>⚠ {warning}</div>)}
                {turn.role === "assistant" && turn.dataQuery && sid && (
                  <DataRating
                    sid={sid}
                    query={turn.dataQuery}
                    onSaved={(feedback) => markFeedback(turn.dataQuery!.logId, feedback)}
                  />
                )}
              </div>
            </div>
            );
          })}
          {((flight.streaming && (traceSteps.length > 0 || flight.live || liveKind))
            || (!flight.streaming && Boolean(flight.error) && traceSteps.length > 0)) && (
            <div className="turn assistant">
              <div className="bubble">
                {traceSteps.length > 0 && <ActivityList steps={traceSteps} />}
                {livePane
                  ? <button type="button" className="result-link" onClick={() => openResult(livePane.id)}>{resultLink(livePane.label)}</button>
                  : flight.live && <Markdown text={flight.live} />}
              </div>
            </div>
          )}
        </div>
        {flight.approvalTool && !flight.streaming && (
          <div className="confirm-bar">
            <span>要执行 {flight.approvalTool} 吗？</span>
            <button className="btn" type="button" disabled={flight.busy} onClick={() => void resumeApproval("skip")}>跳过</button>
            <button className="btn-primary" type="button" disabled={flight.busy} onClick={() => void resumeApproval("confirm")}>确认</button>
          </div>
        )}
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            if (flight.approvalTool) return;
            void send(flight.draft);
          }}
        >
          <textarea
            value={flight.draft}
            placeholder="An order, or attach an agreement (PDF / Word)…"
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onChange={(event) => {
              if (sid) patchFlight(sid, { draft: event.target.value });
            }}
            onKeyDown={(event) => {
              if (event.key !== "Enter" || event.shiftKey) return;
              // keyCode 229: Chrome still delivering this Enter to the IME.
              // Safari reports isComposing false on that same key, so the ref counts too.
              const native = event.nativeEvent;
              if (native.isComposing || native.keyCode === 229 || composing.current) return;
              event.preventDefault();
              void send(flight.draft);
            }}
          />
          <div className="row">
            <select
              aria-label="Model"
              value={modelId}
              onChange={(event) => {
                setModel(event.target.value);
                localStorage.setItem("ka_account.model", event.target.value);
              }}
            >
              {models.map((item) => <option key={item.id} value={item.id}>{item.short}</option>)}
            </select>
            <label className="btn">
              Attach
              <input
                type="file"
                multiple
                hidden
                accept=".png,.jpg,.jpeg,.webp,.pdf,.docx,.txt,.md"
                onChange={(event) => {
                  const picked = [...(event.target.files ?? [])];
                  event.target.value = "";
                  if (!sid) return;
                  const maxImages = desk.meta?.max_images;
                  if (maxImages == null) {
                    patchFlight(sid, { files: picked });
                    return;
                  }
                  const clipped = clipAttachedFiles(picked, maxImages);
                  setAttachNote(clipped.overflow ? `不能再上传图片了，一次最多 ${maxImages} 张` : null);
                  patchFlight(sid, { files: clipped.files });
                }}
              />
            </label>
            {flight.files.length > 0 && <span className="muted">{flight.files.map((file) => file.name).join(", ")}</span>}
            {flight.streaming ? (
              <button className="btn-stop" type="button" aria-label="取消" onClick={() => void cancelTurn()}>
                <StopIcon />
              </button>
            ) : (
              <button className="btn-primary" type="submit" disabled={flight.busy || !sid || Boolean(flight.approvalTool)}>Send</button>
            )}
          </div>
          {attachNote && <div className="warn">{attachNote}</div>}
        </form>
      </section>
      {showPanel && (
        <div
          className="result-resize"
          role="separator"
          aria-orientation="vertical"
          aria-label="调整侧栏宽度"
          onPointerDown={startResize}
        />
      )}
      {showPanel && (
        <aside className="result-float">
          <div className="result-bar">
            <Tabs labels={panes.map((pane) => pane.label)} value={activePane?.label ?? ""} onChange={(label) => {
              const pane = panes.find((item) => item.label === label);
              if (pane) setPicked(pane.id);
            }} />
            <button type="button" className="result-close" aria-label="关闭" onClick={() => setPanelOpen(false)}>×</button>
          </div>
          <div className="panel">
            {activePane?.kind === "order" && (
              activePane.pending
                ? <p className="muted">正在整理采购单…</p>
                : (
                  <>
                    <DraftView draft={snap?.draft ?? null} />
                    {snap?.gaps.map((gap, index) => (
                      <div key={index}>
                        <div className="warn">{gap.message}</div>
                        <div className="actions">
                          {gap.candidates?.map((candidate) => (
                            <button key={candidate.sku} className="btn" type="button" onClick={() => void send(`I mean: ${candidate.name}`, [])}>
                              {candidate.name}
                            </button>
                          ))}
                        </div>
                      </div>
                    ))}
                    {snap?.stage === "confirm" && snap.confirmToken && (
                      <button className="btn-primary" type="button" disabled={flight.busy} onClick={() => void confirm()}>
                        Confirm — place the order
                      </button>
                    )}
                    {snap?.stage === "submitted" && (
                      <p className="ok-note">Order placed. Say “received” or “paid” when the time comes, or start another order.</p>
                    )}
                  </>
                )
            )}
            {activePane?.kind === "report" && (
              activePane.pending
                ? <p className="muted">正在写报告…</p>
                : (
                  <ReportEditor
                    paneId={activePane.id}
                    text={activePane.text}
                    title={reportTitle}
                    charts={activePane.charts}
                    notes={reportNotes}
                    saved={Boolean(artifactTurn?.reportId)}
                    canSave={Boolean(artifactTurn?.reportPendingId)}
                    busy={flight.busy}
                    onTitle={(title) => { if (artifactIndex != null) updateTurn(artifactIndex, { reportTitle: title }); }}
                    onText={(text) => { if (artifactIndex != null) updateTurn(artifactIndex, { text }); }}
                    onConfirm={() => { if (artifactIndex != null) void confirmReport(artifactIndex); }}
                  />
                )
            )}
            {activePane?.kind === "contract" && activePane.filing?.pending_id && (
              <ContractEditor
                filing={activePane.filing}
                busy={flight.busy}
                onChange={(filing) => { if (artifactIndex != null) updateTurn(artifactIndex, { contract: filing }); }}
                onConfirm={() => { if (activePane.filing) void confirmContract(activePane.filing); }}
              />
            )}
            {activePane?.kind === "contract" && !activePane.filing?.pending_id && (
              <ContractResult filing={activePane.filing} pending={activePane.pending} />
            )}
          </div>
        </aside>
      )}
    </div>
  );
}

function StepMark({ state }: { state: Activity["state"] }) {
  if (state === "done") {
    return (
      <span className="step-mark done" aria-hidden="true">
        <svg viewBox="0 0 16 16">
          <circle cx="8" cy="8" r="6.25" />
          <path d="M4.7 8.15 7 10.35 11.3 5.7" />
        </svg>
      </span>
    );
  }
  return (
    <span className="step-mark spin" aria-hidden="true">
      {Array.from({ length: 8 }, (_, index) => (
        <i key={index} style={{ transform: `rotate(${index * 45}deg)` }} />
      ))}
    </span>
  );
}

function ActivityList({ steps }: { steps: Activity[] }) {
  return (
    <ol className="activity">
      {steps.map((step, index) => (
        <li key={step.id} className={step.state}>
          <div className="activity-line">
            <span className="title">{index + 1}. {step.title}</span>
            {step.detail ? <span className="detail">{step.detail}</span> : null}
            <StepMark state={step.state} />
          </div>
        </li>
      ))}
    </ol>
  );
}

function DataRating({
  sid, query, onSaved,
}: {
  sid: string;
  query: DataQueryMark;
  onSaved: (feedback: "good" | "bad") => void;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [sql, setSql] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (query.feedback) {
    return <div className="source">{query.feedback === "good" ? "Marked right" : "Marked wrong"}</div>;
  }

  async function send(feedback: "good" | "bad") {
    setBusy(true);
    setError(null);
    try {
      await postJson(`/api/sessions/${sid}/data-feedback`, {
        log_id: query.logId,
        feedback,
        reason: feedback === "bad" ? reason : null,
        corrected_sql: feedback === "bad" ? sql : null,
      });
      onSaved(feedback);
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rate">
      <button type="button" className="btn" disabled={busy} onClick={() => void send("good")}>Looks right</button>
      <button type="button" className="btn" disabled={busy} onClick={() => setOpen(true)}>Wrong</button>
      {open && (
        <>
          <textarea
            className="rate-note"
            value={reason}
            placeholder="What was wrong (optional)"
            onChange={(event) => setReason(event.target.value)}
          />
          <textarea
            className="rate-note"
            value={sql}
            placeholder="Corrected SQL (optional)"
            onChange={(event) => setSql(event.target.value)}
          />
          <button type="button" className="btn-primary" disabled={busy} onClick={() => void send("bad")}>Save</button>
        </>
      )}
      {error && <div className="warn">{error}</div>}
    </div>
  );
}

function ReportEditor({
  paneId, text, title, charts, notes, saved, canSave, busy, onTitle, onText, onConfirm,
}: {
  paneId: string;
  text: string;
  title: string;
  charts?: ReportChart[];
  notes?: string[];
  saved: boolean;
  canSave: boolean;
  busy: boolean;
  onTitle: (title: string) => void;
  onText: (text: string) => void;
  onConfirm: () => void;
}) {
  const [editing, setEditing] = useState(false);
  useEffect(() => { setEditing(false); }, [paneId]);
  if (!canSave) {
    return (
      <div className="result-doc">
        <Markdown text={text} charts={charts} prose />
        {notes && notes.length > 0 && (
          <div className="refs">
            <div className="refs-label">参考资料：</div>
            {notes.map((note, noteIndex) => (
              <div className="source" key={`${noteIndex}-${note}`}>[{noteIndex + 1}] {note}</div>
            ))}
          </div>
        )}
      </div>
    );
  }
  return (
    <div className="result-doc">
      <label className="field">Title
        <input value={title} placeholder="报告标题" disabled={busy} onChange={(event) => onTitle(event.target.value)} />
      </label>
      <div className="actions">
        <button className="btn" type="button" onClick={() => setEditing((on) => !on)}>{editing ? "Preview" : "Edit"}</button>
      </div>
      {editing
        ? <textarea className="report-edit" value={text} disabled={busy} onChange={(event) => onText(event.target.value)} />
        : <Markdown text={text} charts={charts} prose />}
      {notes && notes.length > 0 && (
        <div className="refs">
          <div className="refs-label">参考资料：</div>
          {notes.map((note, noteIndex) => (
            <div className="source" key={`${noteIndex}-${note}`}>[{noteIndex + 1}] {note}</div>
          ))}
        </div>
      )}
      <div className="confirm-bar">
        <span className="muted">{saved ? "On the Reports page" : "Not saved yet"}</span>
        <button className="btn-primary" type="button" disabled={busy || !text.trim()} onClick={onConfirm}>
          {saved ? "Confirm — update report" : "Confirm — save report"}
        </button>
      </div>
    </div>
  );
}

type DiscountRule = NonNullable<ContractFiling["discount_rules"]>[number];
type DiscountTier = NonNullable<NonNullable<DiscountRule["tiers"]>>[number];

function numOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

function ContractEditor({
  filing, busy, onChange, onConfirm,
}: {
  filing: ContractFiling;
  busy: boolean;
  onChange: (filing: ContractFiling) => void;
  onConfirm: () => void;
}) {
  const rules = filing.discount_rules ?? [];
  const moqs = filing.moqs ?? [];
  const prices = filing.price_list ?? [];
  const territories = filing.territories ?? [];
  const setRule = (index: number, patch: DiscountRule) => {
    onChange({ ...filing, discount_rules: rules.map((rule, i) => (i === index ? patch : rule)) });
  };
  return (
    <>
      <p className="muted">
        {filing.confirmed
          ? "Saved on Contracts. Edit the terms and confirm again to update that agreement."
          : "Nothing is stored until you confirm. PO rules are recalculated from these terms."}
      </p>
      <div className="edit-list">
        <label className="field">Title
          <input value={filing.title ?? ""} disabled={busy} onChange={(event) => onChange({ ...filing, title: event.target.value })} />
        </label>
        <div className="edit-row">
          <label className="field">Type
            <input value={filing.contract_type ?? ""} disabled={busy} onChange={(event) => onChange({ ...filing, contract_type: event.target.value })} />
          </label>
          <label className="field">Start
            <input value={filing.term_start_date ?? ""} disabled={busy} placeholder="YYYY-MM-DD" onChange={(event) => onChange({ ...filing, term_start_date: event.target.value })} />
          </label>
          <label className="field">End
            <input value={filing.term_end_date ?? ""} disabled={busy} placeholder="YYYY-MM-DD" onChange={(event) => onChange({ ...filing, term_end_date: event.target.value })} />
          </label>
        </div>
        <div className="edit-row">
          <label className="field">Auto-renewal
            <select
              value={filing.auto_renewal === true ? "yes" : filing.auto_renewal === false ? "no" : ""}
              disabled={busy}
              onChange={(event) => onChange({
                ...filing,
                auto_renewal: event.target.value === "yes" ? true : event.target.value === "no" ? false : null,
              })}
            >
              <option value="">Unknown</option>
              <option value="yes">Auto-renews</option>
              <option value="no">No auto-renewal</option>
            </select>
          </label>
          <label className="field">Exclusivity
            <input value={filing.exclusivity_type ?? ""} disabled={busy} onChange={(event) => onChange({ ...filing, exclusivity_type: event.target.value })} />
          </label>
        </div>
        <label className="field">Territory
          <textarea value={filing.territory_text ?? ""} disabled={busy} onChange={(event) => onChange({ ...filing, territory_text: event.target.value })} />
        </label>
      </div>
      {filing.skipped && filing.skipped.length > 0 && <div className="warn">Skipped: {filing.skipped.join("; ")}</div>}
      <h3>Discount schedules</h3>
      {rules.length === 0 && <p className="muted">None extracted.</p>}
      {rules.map((rule, index) => (
        <div className="card" key={index}>
          <div className="edit-row">
            <label className="field">Title
              <input value={rule.title ?? ""} disabled={busy} onChange={(event) => setRule(index, { ...rule, title: event.target.value })} />
            </label>
            <label className="field">Section
              <input value={rule.section_reference ?? ""} disabled={busy} onChange={(event) => setRule(index, { ...rule, section_reference: event.target.value })} />
            </label>
          </div>
          <div className="edit-row">
            <label className="field">Applies per
              <input value={rule.applies_per ?? ""} disabled={busy} onChange={(event) => setRule(index, { ...rule, applies_per: event.target.value })} />
            </label>
            <label className="field">Basis
              <input value={rule.basis ?? ""} disabled={busy} onChange={(event) => setRule(index, { ...rule, basis: event.target.value })} />
            </label>
          </div>
          {(rule.tiers ?? []).map((tier, tierIndex) => (
            <div className="edit-row" key={tierIndex}>
              <label className="field">From
                <input value={tier.from_container ?? ""} disabled={busy} onChange={(event) => setRule(index, { ...rule, tiers: replaceAt(rule.tiers, tierIndex, { ...tier, from_container: numOrNull(event.target.value) }) })} />
              </label>
              <label className="field">To
                <input value={tier.to_container ?? ""} disabled={busy} placeholder="∞" onChange={(event) => setRule(index, { ...rule, tiers: replaceAt(rule.tiers, tierIndex, { ...tier, to_container: numOrNull(event.target.value) }) })} />
              </label>
              <label className="field">Discount %
                <input value={tier.discount_percent ?? ""} disabled={busy} onChange={(event) => setRule(index, { ...rule, tiers: replaceAt(rule.tiers, tierIndex, { ...tier, discount_percent: numOrNull(event.target.value) }) })} />
              </label>
              <button className="btn" type="button" disabled={busy} onClick={() => setRule(index, { ...rule, tiers: (rule.tiers ?? []).filter((_, i) => i !== tierIndex) })}>Remove</button>
            </div>
          ))}
          <button
            className="btn"
            type="button"
            disabled={busy}
            onClick={() => setRule(index, { ...rule, tiers: [...(rule.tiers ?? []), blankTier()] })}
          >Add tier</button>
        </div>
      ))}
      <button className="btn" type="button" disabled={busy} onClick={() => onChange({ ...filing, discount_rules: [...rules, { title: "", tiers: [blankTier()] }] })}>Add schedule</button>
      <h3>Minimum order quantities</h3>
      {moqs.length === 0 && <p className="muted">None extracted.</p>}
      {moqs.map((row, index) => (
        <div className="edit-row" key={index}>
          <label className="field">Quantity
            <input value={String(row.quantity ?? "")} disabled={busy} onChange={(event) => onChange({ ...filing, moqs: replaceAt(moqs, index, { ...row, quantity: numOrNull(event.target.value) }) })} />
          </label>
          <label className="field">Unit
            <input value={String(row.unit ?? "")} disabled={busy} onChange={(event) => onChange({ ...filing, moqs: replaceAt(moqs, index, { ...row, unit: event.target.value }) })} />
          </label>
          <label className="field">Applies per
            <input value={String(row.applies_per ?? "")} disabled={busy} onChange={(event) => onChange({ ...filing, moqs: replaceAt(moqs, index, { ...row, applies_per: event.target.value }) })} />
          </label>
          <label className="field">SKU
            <input value={String(row.sku ?? "")} disabled={busy} onChange={(event) => onChange({ ...filing, moqs: replaceAt(moqs, index, { ...row, sku: event.target.value }) })} />
          </label>
          <button className="btn" type="button" disabled={busy} onClick={() => onChange({ ...filing, moqs: moqs.filter((_, i) => i !== index) })}>Remove</button>
        </div>
      ))}
      <button className="btn" type="button" disabled={busy} onClick={() => onChange({ ...filing, moqs: [...moqs, { quantity: null, unit: "", applies_per: "per_order" }] })}>Add minimum</button>
      <h3>Price list</h3>
      {prices.length === 0 && <p className="muted">None extracted.</p>}
      {prices.map((row, index) => (
        <div className="edit-row" key={index}>
          <label className="field">Product
            <input value={String(row.product_name ?? "")} disabled={busy} onChange={(event) => onChange({ ...filing, price_list: replaceAt(prices, index, { ...row, product_name: event.target.value }) })} />
          </label>
          <label className="field">SKU
            <input value={String(row.product_sku ?? "")} disabled={busy} onChange={(event) => onChange({ ...filing, price_list: replaceAt(prices, index, { ...row, product_sku: event.target.value }) })} />
          </label>
          <label className="field">Price
            <input value={String(row.unit_price ?? "")} disabled={busy} onChange={(event) => onChange({ ...filing, price_list: replaceAt(prices, index, { ...row, unit_price: numOrNull(event.target.value) }) })} />
          </label>
          <label className="field">Currency
            <input value={String(row.currency ?? "")} disabled={busy} onChange={(event) => onChange({ ...filing, price_list: replaceAt(prices, index, { ...row, currency: event.target.value }) })} />
          </label>
          <button className="btn" type="button" disabled={busy} onClick={() => onChange({ ...filing, price_list: prices.filter((_, i) => i !== index) })}>Remove</button>
        </div>
      ))}
      <button className="btn" type="button" disabled={busy} onClick={() => onChange({ ...filing, price_list: [...prices, { product_name: "", unit_price: null, currency: "" }] })}>Add price</button>
      <h3>Territories</h3>
      {territories.length === 0 && <p className="muted">None extracted.</p>}
      {territories.map((row, index) => (
        <div className="edit-row" key={index}>
          <label className="field">Country
            <input value={row.country_code ?? ""} disabled={busy} onChange={(event) => onChange({ ...filing, territories: replaceAt(territories, index, { ...row, country_code: event.target.value }) })} />
          </label>
          <label className="field">Kind
            <select
              value={row.kind === "excluded" ? "excluded" : "allowed"}
              disabled={busy}
              onChange={(event) => onChange({ ...filing, territories: replaceAt(territories, index, { ...row, kind: event.target.value }) })}
            >
              <option value="allowed">Allowed</option>
              <option value="excluded">Excluded</option>
            </select>
          </label>
          <button className="btn" type="button" disabled={busy} onClick={() => onChange({ ...filing, territories: territories.filter((_, i) => i !== index) })}>Remove</button>
        </div>
      ))}
      <button className="btn" type="button" disabled={busy} onClick={() => onChange({ ...filing, territories: [...territories, { country_code: "", kind: "allowed" }] })}>Add territory</button>
      <h3>PO rules</h3>
      {(filing.derived_rules ?? []).length === 0 ? <p className="muted">No rules derived yet.</p> : (
        <DataTable
          rows={filing.derived_rules ?? []}
          columns={[
            { key: "name", label: "Rule" },
            { key: "rule_type", label: "Type" },
            { key: "severity", label: "Severity" },
            { key: "rule_config", label: "Config", wrap: true, render: (row) => JSON.stringify(row.rule_config ?? {}) },
          ]}
        />
      )}
      <div className="confirm-bar">
        <span className="muted">{filing.confirmed ? "On the Contracts page" : "Not saved yet"}</span>
        <button className="btn-primary" type="button" disabled={busy} onClick={onConfirm}>
          {filing.confirmed ? "Confirm — update contract" : "Confirm — file contract"}
        </button>
      </div>
    </>
  );
}

function blankTier(): DiscountTier {
  return { from_container: null, to_container: null, discount_percent: null, notes: null };
}

function replaceAt<T>(rows: T[] | undefined, index: number, next: T): T[] {
  return (rows ?? []).map((row, i) => (i === index ? next : row));
}

function ContractResult({ filing, pending }: { filing: ContractFiling | null; pending?: boolean }) {
  if (pending || !filing) return <p className="muted">正在读取合同…</p>;
  const rules = filing.discount_rules ?? [];
  const moqs = filing.moqs ?? [];
  const prices = filing.price_list ?? [];
  const derived = filing.derived_rules ?? [];
  const caption = [
    filing.contract_type?.replaceAll("_", " "),
    filing.file_name,
    (filing.term_start_date || filing.term_end_date) ? `${filing.term_start_date || "?"} → ${filing.term_end_date || "?"}` : null,
    filing.auto_renewal === true ? "auto-renews" : filing.auto_renewal === false ? "no auto-renewal" : null,
    filing.exclusivity_type,
  ].filter(Boolean).join(" · ");
  return (
    <>
      {caption && <p className="muted">{caption}</p>}
      {filing.territory_text && <p><strong>Territory</strong> {filing.territory_text}</p>}
      {filing.territories && filing.territories.length > 0 && (
        <p>
          {filing.territories.map((territory) => (
            <span key={`${territory.kind}-${territory.country_code}`} className={`badge ${territory.kind === "allowed" ? "ok" : "bad"}`} style={{ marginRight: 6 }}>
              {territory.country_code}{territory.kind === "excluded" ? " excluded" : ""}
            </span>
          ))}
        </p>
      )}
      {filing.skipped && filing.skipped.length > 0 && <div className="warn">Skipped: {filing.skipped.join("; ")}</div>}
      <h3>Discount schedules</h3>
      {rules.length === 0 && <p className="muted">None extracted.</p>}
      {rules.map((rule, index) => (
        <div className="card" key={index}>
          <strong>{rule.title || "Discount rule"}</strong>
          {rule.section_reference ? ` · §${rule.section_reference}` : ""}
          <div className="muted">{(rule.applies_per || "").replaceAll("_", " ")} · {(rule.basis || "").replaceAll("_", " ")}</div>
          <DataTable
            rows={rule.tiers ?? []}
            columns={[
              { key: "containers", label: "Containers", render: (tier) => `${tier.from_container || 1} – ${tier.to_container ?? "∞"}` },
              { key: "discount", label: "Discount", render: (tier) => tier.discount_percent == null ? "—" : `${trimNum(tier.discount_percent)}%` },
            ]}
          />
        </div>
      ))}
      {moqs.length > 0 && (
        <>
          <h3>Minimum order quantities</h3>
          <DataTable rows={moqs} columns={Object.keys(moqs[0]).map((key) => ({ key, label: key }))} />
        </>
      )}
      {prices.length > 0 && (
        <>
          <h3>Price list</h3>
          <DataTable rows={prices} columns={Object.keys(prices[0]).map((key) => ({ key, label: key }))} />
        </>
      )}
      <h3>PO rules</h3>
      {derived.length === 0 ? <p className="muted">No rules derived.</p> : (
        <DataTable
          rows={derived}
          columns={[
            { key: "name", label: "Rule" },
            { key: "rule_type", label: "Type" },
            { key: "severity", label: "Severity" },
            { key: "rule_config", label: "Config", wrap: true, render: (row) => JSON.stringify(row.rule_config ?? {}) },
          ]}
        />
      )}
    </>
  );
}

function DraftView({ draft }: { draft: Draft | null }) {
  if (!draft) return <p className="muted">Nothing yet.</p>;
  const cur = draft.currency;
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead>
            <tr><th>Product</th><th>SKU</th><th>Cases</th><th>Price</th><th>Total</th><th /></tr>
          </thead>
          <tbody>
            {draft.lines.map((line, index) => (
              <tr key={index}>
                <td>{line.name}</td>
                <td>{line.sku || line.reference}</td>
                <td>{line.cases}</td>
                <td>{money(cur, line.case_price)}</td>
                <td>{money(cur, line.line_total)}</td>
                <td>{lineFlag(line)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p>
        <strong>Payment</strong> {draft.payment_terms}<br />
        <strong>Incoterms</strong> {draft.incoterms}<br />
        <strong>Shipping</strong> {draft.shipping_method}
      </p>
      <p>
        <strong>ETA</strong> {draft.eta_date || "—"}<br />
        <strong>Discount</strong> {discountText(draft.discount, cur)}
        {draft.notes ? <><br /><strong>Notes</strong> {draft.notes}</> : null}
      </p>
      <h3>Total {money(cur, draft.total)}</h3>
    </>
  );
}
