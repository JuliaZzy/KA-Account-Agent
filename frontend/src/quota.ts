/** Fixed desk copy. The model is not asked to say this. */

export const QUOTA_LOCK_COPY = "今天的额度已经用完，请明天再试。";
export const QUOTA_TURN_COPY = "这一轮的模型额度已经用完。";

export type QuotaSignal = {
  exhausted?: boolean;
  scope?: string | null;
  code?: string | null;
};

/** Org daily quota locks the composer. A single run's safety net does not. */
export function quotaLocked(input: QuotaSignal): boolean {
  if (input.exhausted) return true;
  if (input.scope === "org") return true;
  return input.code === "QUOTA_EXCEEDED" && input.scope !== "run";
}

export function quotaNotice(input: QuotaSignal): string | null {
  if (quotaLocked(input)) return QUOTA_LOCK_COPY;
  if (input.scope === "run") return QUOTA_TURN_COPY;
  return null;
}
