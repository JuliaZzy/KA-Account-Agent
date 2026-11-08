export class ApiError extends Error {}

async function readError(res: Response): Promise<string> {
  const text = await res.text();
  try {
    const body = JSON.parse(text) as { detail?: unknown; error?: { message?: unknown } };
    if (typeof body.detail === "string") return body.detail;
    if (typeof body.error?.message === "string") return body.error.message;
  } catch {
    /* not JSON */
  }
  return text || res.statusText;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) throw new ApiError(await readError(res));
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export function postJson<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function postForm<T>(path: string, file: File, fields: Record<string, string>): Promise<T> {
  const body = new FormData();
  body.append("file", file);
  for (const [key, value] of Object.entries(fields)) body.append(key, value);
  return api<T>(path, { method: "POST", body });
}

export function resumeRun(
  runId: string,
  orgId: string,
  userId: string,
  input: { message: string; decision: "confirm" | "skip" },
): Promise<AgentRun> {
  return api<AgentRun>(`/api/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Org-Id": orgId,
      "X-User-Id": userId,
    },
    body: JSON.stringify({ input }),
  });
}

export function cancelRun(runId: string, orgId: string, userId: string): Promise<void> {
  return api<void>(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    headers: { "X-Org-Id": orgId, "X-User-Id": userId },
  });
}

export type AgentRun = {
  id: string;
  status: string;
  output_json?: { text?: string; generation_id?: string } | null;
  error_json?: { message?: string; kind?: string } | null;
};

export function getRun(runId: string, orgId: string, userId: string): Promise<AgentRun> {
  return api<AgentRun>(`/api/runs/${encodeURIComponent(runId)}`, {
    headers: { "X-Org-Id": orgId, "X-User-Id": userId },
  });
}

export function getRunTrace(runId: string, orgId: string, userId: string): Promise<RunTrace> {
  return api<RunTrace>(`/api/runs/${encodeURIComponent(runId)}/trace`, {
    headers: { "X-Org-Id": orgId, "X-User-Id": userId },
  });
}

export type RunTrace = {
  run: AgentRun;
  steps: unknown;
  events?: { event_type: string; payload_json?: { tool_name?: string } }[];
};

export type RunEvent = {
  sequence: number;
  event_type: string;
  payload_json?: {
    tool_name?: string;
    display?: string;
    text?: string;
    reasoning?: string;
    generation_id?: string;
  };
};

const RUN_SETTLED = new Set(["succeeded", "failed", "cancelled", "waiting_for_user"]);

function authHeaders(orgId: string, userId: string): HeadersInit {
  return { "X-Org-Id": orgId, "X-User-Id": userId };
}

export function getRunEvents(runId: string, orgId: string, userId: string, after = 0): Promise<RunEvent[]> {
  const query = after > 0 ? `?after=${after}` : "";
  return api<RunEvent[]>(`/api/runs/${encodeURIComponent(runId)}/events${query}`, {
    headers: authHeaders(orgId, userId),
  });
}

function parseSse(chunk: string): RunEvent | null {
  let sequence = 0;
  let eventType = "";
  let data = "";
  for (const line of chunk.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("id:")) sequence = Number(line.slice(3).trim());
    else if (line.startsWith("event:")) eventType = line.slice(6).trim();
    else if (line.startsWith("data:")) data = line.slice(5).trim();
  }
  if (!eventType || !Number.isInteger(sequence) || sequence <= 0) return null;
  let payload: RunEvent["payload_json"] = {};
  if (data) {
    try {
      const parsed = JSON.parse(data) as RunEvent["payload_json"];
      if (parsed && typeof parsed === "object") payload = parsed;
    } catch {
      payload = {};
    }
  }
  return { sequence, event_type: eventType, payload_json: payload };
}

/** Load the run and its public events, then follow the stream until it settles.

The activity list comes from events. Reply text is read from the run, because
writing that text does not append a `run_events` row.
*/
export async function followRun(
  runId: string,
  orgId: string,
  userId: string,
  signal?: AbortSignal,
  onUpdate?: (events: RunEvent[], run: AgentRun) => void,
): Promise<AgentRun> {
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  const local = new AbortController();
  const stop = () => local.abort();
  signal?.addEventListener("abort", stop);
  const seen: RunEvent[] = [];
  const known = new Set<number>();
  let after = 0;
  let run = await getRun(runId, orgId, userId);

  function add(events: RunEvent[]) {
    for (const event of events) {
      if (!Number.isInteger(event.sequence) || known.has(event.sequence)) continue;
      known.add(event.sequence);
      seen.push(event);
      if (event.sequence > after) after = event.sequence;
    }
    seen.sort((left, right) => left.sequence - right.sequence);
  }

  add(await getRunEvents(runId, orgId, userId, 0));
  onUpdate?.(seen, run);
  if (RUN_SETTLED.has(run.status)) {
    signal?.removeEventListener("abort", stop);
    return run;
  }

  const headers = authHeaders(orgId, userId);
  let closed = false;
  const pump = (async () => {
    while (!closed && !local.signal.aborted) {
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/events/stream?after=${after}`, {
          headers,
          signal: local.signal,
        });
        if (!res.ok || !res.body) throw new Error("stream closed");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (closed || local.signal.aborted) return;
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const chunks = buf.split("\n\n");
          buf = chunks.pop() ?? "";
          const before = seen.length;
          for (const chunk of chunks) {
            const event = parseSse(chunk);
            if (event) add([event]);
          }
          if (seen.length !== before) onUpdate?.(seen, run);
        }
      } catch {
        if (closed || local.signal.aborted) return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 400));
    }
  })();
  void pump.catch(() => undefined);

  try {
    for (;;) {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      run = await getRun(runId, orgId, userId);
      if (RUN_SETTLED.has(run.status)) {
        add(await getRunEvents(runId, orgId, userId, after));
        onUpdate?.(seen, run);
        return run;
      }
      onUpdate?.(seen, run);
      await new Promise((resolve) => {
        const timer = window.setTimeout(resolve, 800);
        const abort = () => {
          window.clearTimeout(timer);
          resolve(undefined);
        };
        signal?.addEventListener("abort", abort, { once: true });
      });
    }
  } finally {
    closed = true;
    local.abort();
    signal?.removeEventListener("abort", stop);
  }
}

export async function streamMessage(
  sid: string,
  body: unknown,
  onEvent: (event: Record<string, unknown>) => void | Promise<void>,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api/sessions/${sid}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new ApiError(await readError(res));
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const chunks = buf.split("\n\n");
    buf = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((item) => item.startsWith("data: "));
      if (line) await onEvent(JSON.parse(line.slice(6)) as Record<string, unknown>);
    }
  }
}
