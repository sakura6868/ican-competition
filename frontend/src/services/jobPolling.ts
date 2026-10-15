const MIN_RETRY_DELAY_MS = 1_000;
const MAX_RETRY_DELAY_MS = 8_000;

/** Back off transient polling failures without making a short disconnect fatal. */
export function pollingRetryDelayMs(consecutiveFailures: number): number {
  const safeFailures = Number.isFinite(consecutiveFailures)
    ? Math.max(1, Math.floor(consecutiveFailures))
    : 1;
  return Math.min(MAX_RETRY_DELAY_MS, MIN_RETRY_DELAY_MS * (2 ** (safeFailures - 1)));
}

export function isActiveJobStatus(status: string | null | undefined): boolean {
  return status === "queued" || status === "running" || status === "retrying";
}

export function isTerminalJobStatus(status: string | null | undefined): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}
