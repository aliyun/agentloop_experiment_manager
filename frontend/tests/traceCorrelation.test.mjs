import assert from "node:assert/strict";
import test from "node:test";
import { traceCorrelationFromApi } from "../src/lib/traceCorrelation.ts";

test("normalizes the camelCase Trace correlation summary", () => {
  assert.deepEqual(traceCorrelationFromApi({
    state: "POLLING",
    traceIds: ["ABCDEF0123456789ABCDEF0123456789"],
    totalTasks: 4,
    pendingTasks: 2,
    resolvedTasks: 2,
    reportedTasks: 1,
    failedTasks: 0,
    totalAttempts: 5,
    lastErrorCode: null,
    lastErrorMessage: null,
    startedAt: "2026-08-09T01:02:03Z",
    finishedAt: null,
  }), {
    state: "POLLING",
    traceIds: ["abcdef0123456789abcdef0123456789"],
    totalTasks: 4,
    pendingTasks: 2,
    resolvedTasks: 2,
    reportedTasks: 1,
    failedTasks: 0,
    totalAttempts: 5,
    lastErrorCode: null,
    lastErrorMessage: null,
    startedAt: "2026-08-09T01:02:03Z",
    finishedAt: null,
  });
});

test("normalizes legacy snake_case fields and defensive count values", () => {
  assert.deepEqual(traceCorrelationFromApi({
    correlation_state: "REPORTED",
    trace_ids: ["0123456789abcdef0123456789abcdef", "invalid", "0123456789ABCDEF0123456789ABCDEF"],
    total_tasks: "3",
    pending_tasks: -1,
    resolved_tasks: 3.8,
    reported_tasks: "3",
    failed_tasks: "invalid",
    total_attempts: 7,
    last_error_code: "",
    last_error_message: "old warning",
    started_at: "2026-08-09T01:02:03Z",
    finished_at: "2026-08-09T01:02:12Z",
  }), {
    state: "REPORTED",
    traceIds: ["0123456789abcdef0123456789abcdef"],
    totalTasks: 3,
    pendingTasks: 0,
    resolvedTasks: 3,
    reportedTasks: 3,
    failedTasks: 0,
    totalAttempts: 7,
    lastErrorCode: "",
    lastErrorMessage: "old warning",
    startedAt: "2026-08-09T01:02:03Z",
    finishedAt: "2026-08-09T01:02:12Z",
  });
});

test("keeps an absent summary absent instead of presenting false progress", () => {
  assert.equal(traceCorrelationFromApi(undefined), undefined);
  assert.equal(traceCorrelationFromApi(null), undefined);
  assert.equal(traceCorrelationFromApi([]), undefined);
});

test("defaults absent or malformed trace IDs to an empty list", () => {
  assert.deepEqual(traceCorrelationFromApi({ state: "NOT_STARTED", traceIds: "not-an-array" })?.traceIds, []);
});
