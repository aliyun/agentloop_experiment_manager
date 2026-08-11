import type { TraceCorrelationSummary } from "./types";

function count(value: unknown): number {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
}

function nullableText(value: unknown): string | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  return String(value);
}

function field(raw: Record<string, unknown>, ...names: string[]): unknown {
  const name = names.find((candidate) => Object.prototype.hasOwnProperty.call(raw, candidate));
  return name ? raw[name] : undefined;
}

function traceIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const normalized = value
    .map((item) => String(item).trim().toLowerCase())
    .filter((item) => /^[0-9a-f]{32}$/.test(item));
  return [...new Set(normalized)];
}

/** Normalize the nested launch summary while older servers still return snake_case. */
export function traceCorrelationFromApi(value: unknown): TraceCorrelationSummary | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  return {
    state: String(field(raw, "state", "correlationState", "correlation_state") ?? "UNKNOWN"),
    traceIds: traceIds(field(raw, "traceIds", "trace_ids")),
    totalTasks: count(field(raw, "totalTasks", "total_tasks")),
    pendingTasks: count(field(raw, "pendingTasks", "pending_tasks")),
    resolvedTasks: count(field(raw, "resolvedTasks", "resolved_tasks")),
    reportedTasks: count(field(raw, "reportedTasks", "reported_tasks")),
    failedTasks: count(field(raw, "failedTasks", "failed_tasks")),
    totalAttempts: count(field(raw, "totalAttempts", "total_attempts")),
    lastErrorCode: nullableText(field(raw, "lastErrorCode", "last_error_code")),
    lastErrorMessage: nullableText(field(raw, "lastErrorMessage", "last_error_message")),
    startedAt: nullableText(field(raw, "startedAt", "started_at")),
    finishedAt: nullableText(field(raw, "finishedAt", "finished_at")),
  };
}

export function isTraceCorrelationTerminal(state?: string | null): boolean {
  return ["REPORTED", "AMBIGUOUS", "TIMED_OUT", "FAILED"].includes(
    String(state ?? "").toUpperCase(),
  );
}
