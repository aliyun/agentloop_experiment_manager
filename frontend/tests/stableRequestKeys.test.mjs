import assert from "node:assert/strict";
import test from "node:test";
import {
  shouldCompleteStableRequest,
  shouldDiscardStableRequestAfterApiError,
  StableRequestKeys,
} from "../src/lib/stableRequestKeys.ts";

test("reuses a request key after failure and clears it only after observed success", () => {
  let sequence = 0;
  const keys = new StableRequestKeys(() => `key-${++sequence}`);

  const first = keys.keyFor("schedule-a");
  assert.equal(first, "key-1");
  assert.equal(keys.keyFor("schedule-a"), first, "retry must reuse the logical request key");

  keys.complete("schedule-a", first);
  assert.equal(keys.keyFor("schedule-a"), "key-2", "a new attempt starts only after success");
});

test("keeps request keys isolated by schedule", () => {
  let sequence = 0;
  const keys = new StableRequestKeys(() => `key-${++sequence}`);
  assert.equal(keys.keyFor("schedule-a"), "key-1");
  assert.equal(keys.keyFor("schedule-b"), "key-2");
  assert.equal(keys.keyFor("schedule-a"), "key-1");
});

test("completes stable keys only for known submission outcomes", () => {
  assert.equal(shouldCompleteStableRequest("ACCEPTED"), true);
  assert.equal(shouldCompleteStableRequest("SKIPPED"), true);
  assert.equal(shouldCompleteStableRequest("DEFINITIVE_FAILURE"), true);
  assert.equal(shouldCompleteStableRequest("SUBMISSION_UNKNOWN"), false);
  assert.equal(shouldCompleteStableRequest("SUBMITTING"), false);
  assert.equal(shouldCompleteStableRequest(undefined), false);
});

test("discards keys for explicit API failures but retains indeterminate responses", () => {
  assert.equal(shouldDiscardStableRequestAfterApiError({ status: 503, code: "AGENTLOOP_TIMEOUT" }), true);
  assert.equal(shouldDiscardStableRequestAfterApiError({ status: 409, code: "LOCAL_CONCURRENCY_LIMIT" }), true);
  assert.equal(shouldDiscardStableRequestAfterApiError({ status: 422, code: "UNKNOWN_ERROR" }), true);

  assert.equal(shouldDiscardStableRequestAfterApiError({ status: 503, code: "SUBMISSION_UNKNOWN" }), false);
  assert.equal(shouldDiscardStableRequestAfterApiError({ status: 503, code: "UNKNOWN_ERROR" }), false);
  assert.equal(shouldDiscardStableRequestAfterApiError({ status: 504, code: null }), false);
});
