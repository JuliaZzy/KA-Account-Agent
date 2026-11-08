import { beforeEach, describe, expect, it } from "vitest";

import { chatPresence, flightOf, forgetChat, isChatBusy, isForgotten, liveFromEvents, liveFromRun, patchFlight, resetChatLive, setActiveChat } from "./chat-live";

describe("per-chat live state", () => {
  beforeEach(() => resetChatLive());

  it("keeps a reply and a draft on the chat that owns them", () => {
    patchFlight("a", { busy: true, live: "one", draft: "still typing" });
    patchFlight("b", { draft: "other" });
    expect(flightOf("a")).toMatchObject({ busy: true, live: "one", draft: "still typing" });
    expect(flightOf("b")).toMatchObject({ busy: false, live: "", draft: "other" });
    expect(isChatBusy("a")).toBe(true);
    expect(isChatBusy("b")).toBe(false);
    expect(chatPresence().busyIds.slice().sort()).toEqual(["a"]);
  });

  it("a token on one chat does not replace the other's flight or the sidebar snapshot", () => {
    setActiveChat("a");
    patchFlight("a", { busy: true, live: "one" });
    const flight = flightOf("a");
    const sidebar = chatPresence();
    patchFlight("a", { live: "one more" });
    expect(flightOf("a")).not.toBe(flight);
    expect(flightOf("b")).toMatchObject({ busy: false, live: "" });
    expect(chatPresence()).toBe(sidebar);
    expect(chatPresence().activeId).toBe("a");
  });

  it("keeps each chat's activity list", () => {
    const shorter = [{ id: "1", title: "已开始", detail: "", state: "done" as const }];
    const longer = [
      { id: "1", title: "已开始", detail: "", state: "done" as const },
      { id: "2", title: "正在思考", detail: "", state: "running" as const },
    ];
    patchFlight("a", { streaming: true, steps: shorter });
    patchFlight("b", { streaming: true, steps: longer });
    patchFlight("a", { steps: shorter });
    expect(flightOf("a").steps).toEqual(shorter);
    expect(flightOf("b").steps).toEqual(longer);
  });

  it("a new generation_id drops tokens from a failed stream", () => {
    patchFlight("a", { busy: true, live: "Hel", generationId: "gen-1" });
    const cleared = liveFromRun(flightOf("a"), { text: "", generation_id: "gen-2" });
    expect(cleared).toEqual({ generationId: "gen-2", live: "" });
    patchFlight("a", cleared);
    const next = liveFromRun(flightOf("a"), { text: "你好", generation_id: "gen-2" });
    expect(next).toEqual({ live: "你好" });
  });

  it("reads the latest reply from durable SSE events", () => {
    patchFlight("a", { generationId: "gen-1", live: "旧" });
    expect(liveFromEvents(flightOf("a"), [
      { event_type: "run.reply_updated", payload_json: { text: "新", generation_id: "gen-1" } },
    ])).toEqual({ live: "新" });
  });

  it("a deleted chat ignores a reply that arrives late", () => {
    setActiveChat("a");
    patchFlight("a", { busy: true, live: "one" });
    patchFlight("b", { live: "keep" });
    forgetChat("a");
    patchFlight("a", { live: "late", busy: true });
    expect(isForgotten("a")).toBe(true);
    expect(isChatBusy("a")).toBe(false);
    expect(flightOf("a").live).toBe("");
    expect(flightOf("b").live).toBe("keep");
    expect(chatPresence().activeId).toBeNull();
    expect(chatPresence().busyIds).toEqual([]);
  });
});
