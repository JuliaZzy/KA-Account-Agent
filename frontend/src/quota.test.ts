import { describe, expect, it } from "vitest";

import { QUOTA_LOCK_COPY, QUOTA_TURN_COPY, quotaLocked, quotaNotice } from "./quota";

describe("quota notice", () => {
  it("locks the composer for the daily org cap", () => {
    expect(quotaLocked({ exhausted: true })).toBe(true);
    expect(quotaNotice({ exhausted: true })).toBe(QUOTA_LOCK_COPY);
    expect(quotaNotice({ code: "QUOTA_EXCEEDED", scope: "org" })).toBe(QUOTA_LOCK_COPY);
  });

  it("keeps send available when only this run's safety net was spent", () => {
    expect(quotaLocked({ scope: "run" })).toBe(false);
    expect(quotaNotice({ scope: "run" })).toBe(QUOTA_TURN_COPY);
  });
});
