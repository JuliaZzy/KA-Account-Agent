import { useSyncExternalStore } from "react";

import type { Activity } from "./format";

/** One chat's composer and the reply that is still coming in. Other chats keep their own. */
export type Flight = {
  busy: boolean;
  /** True only while a chat reply stream is open. Confirm and save stay busy without this. */
  streaming: boolean;
  /** Run id from the stream. The cancel click and the trace poll use it. */
  runId: string | null;
  live: string;
  /** Labels one model attempt. A new id means the previous live tokens are stale. */
  generationId: string | null;
  error: string | null;
  draft: string;
  files: File[];
  task: "po" | "report" | "contract" | null;
  tasks: ("po" | "report" | "contract")[];
  /** Tool name while the run is parked for confirm or skip. */
  approvalTool: string | null;
  /** This reply's activity rows. A second chat's poll must not replace them. */
  steps: Activity[];
};

const EMPTY: Flight = {
  busy: false,
  streaming: false,
  runId: null,
  live: "",
  generationId: null,
  error: null,
  draft: "",
  files: [],
  task: null,
  tasks: [],
  approvalTool: null,
  steps: [],
};

const flights = new Map<string, Flight>();
const dropped = new Set<string>();
const forgetListeners = new Set<(id: string) => void>();
let activeId: string | null = null;
let presence: { activeId: string | null; busyIds: string[] } = { activeId, busyIds: [] };
const flightListeners = new Set<() => void>();
const presenceListeners = new Set<() => void>();

function emitFlight(): void {
  for (const listener of flightListeners) listener();
}

function emitPresence(): void {
  const busyIds: string[] = [];
  for (const [id, flight] of flights) {
    if (flight.busy) busyIds.push(id);
  }
  presence = { activeId, busyIds };
  for (const listener of presenceListeners) listener();
}

export function flightOf(sid: string | null): Flight {
  if (!sid) return EMPTY;
  return flights.get(sid) ?? EMPTY;
}

export function isForgotten(sid: string): boolean {
  return dropped.has(sid);
}

export function forgetChat(sid: string): void {
  dropped.add(sid);
  flights.delete(sid);
  if (activeId === sid) activeId = null;
  emitFlight();
  emitPresence();
  for (const listener of forgetListeners) listener(sid);
}

export function onForgetChat(listener: (id: string) => void): () => void {
  forgetListeners.add(listener);
  return () => forgetListeners.delete(listener);
}

export function patchFlight(sid: string, patch: Partial<Flight>): void {
  if (dropped.has(sid)) return;
  const prev = flights.get(sid) ?? EMPTY;
  const next: Flight = { ...prev, ...patch };
  flights.set(sid, next);
  emitFlight();
  if (prev.busy !== next.busy) emitPresence();
}

export function isChatBusy(sid: string): boolean {
  return flights.get(sid)?.busy ?? false;
}

export function setActiveChat(sid: string | null): void {
  if (activeId === sid) return;
  activeId = sid;
  emitPresence();
}

export function chatPresence(): { activeId: string | null; busyIds: string[] } {
  return presence;
}

function subscribeFlight(listener: () => void): () => void {
  flightListeners.add(listener);
  return () => flightListeners.delete(listener);
}

function subscribePresence(listener: () => void): () => void {
  presenceListeners.add(listener);
  return () => presenceListeners.delete(listener);
}

export function useFlight(sid: string | null): Flight {
  return useSyncExternalStore(subscribeFlight, () => flightOf(sid), () => EMPTY);
}

export function useChatPresence(): { activeId: string | null; busyIds: string[] } {
  return useSyncExternalStore(subscribePresence, () => presence, () => presence);
}

export function liveFromRun(
  flight: Flight,
  output: { text?: string; generation_id?: string } | null | undefined,
): Partial<Flight> {
  const generationId = output?.generation_id ?? null;
  const partial = output?.text;
  if (generationId && generationId !== flight.generationId) {
    return { generationId, live: typeof partial === "string" ? partial : "" };
  }
  if (typeof partial === "string" && partial) {
    return { live: partial };
  }
  return {};
}

export function resetChatLive(): void {
  flights.clear();
  dropped.clear();
  activeId = null;
  presence = { activeId: null, busyIds: [] };
  emitFlight();
  emitPresence();
}
