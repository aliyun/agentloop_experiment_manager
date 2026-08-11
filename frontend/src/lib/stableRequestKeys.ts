/** Keep one browser request key for each in-flight logical action until success is observed. */
export class StableRequestKeys {
  private readonly keys = new Map<string, string>();
  private readonly createKey: () => string;

  constructor(createKey: () => string) {
    this.createKey = createKey;
  }

  keyFor(scope: string): string {
    const existing = this.keys.get(scope);
    if (existing) return existing;
    const created = this.createKey();
    this.keys.set(scope, created);
    return created;
  }

  complete(scope: string, completedKey: string): void {
    if (this.keys.get(scope) === completedKey) this.keys.delete(scope);
  }

  discard(scope: string): void {
    this.keys.delete(scope);
  }
}

/** A browser request key may be retired only after the server reports a known outcome. */
export function shouldCompleteStableRequest(submissionState?: string | null): boolean {
  return ["ACCEPTED", "SKIPPED", "DEFINITIVE_FAILURE"].includes((submissionState || "").toUpperCase());
}

const DEFINITIVE_SERVER_ERROR_CODES = new Set([
  "AGENTLOOP_GATEWAY_ERROR",
  "AGENTLOOP_TIMEOUT",
  "AGENTLOOP_UNAVAILABLE",
  "CONNECTION_NOT_CONFIGURED",
  "CONNECTION_NOT_VALIDATED",
  "CREDENTIALS_UNAVAILABLE",
  "EXPERIMENT_PREPARATION_FAILED",
  "LAUNCH_DEFINITIVE_FAILURE",
]);

/**
 * Decide whether a received non-2xx response proves that the logical request may end.
 * Transport failures never produce ApiError and therefore never reach this helper. Unknown
 * 5xx/proxy responses remain indeterminate; only explicit application codes may release a key.
 */
export function shouldDiscardStableRequestAfterApiError(error: { status: number; code?: string | null }): boolean {
  const code = (error.code || "").toUpperCase();
  if (code === "SUBMISSION_UNKNOWN") return false;
  if (error.status >= 400 && error.status < 500) return true;
  return error.status >= 500 && DEFINITIVE_SERVER_ERROR_CODES.has(code);
}
