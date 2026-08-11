import assert from "node:assert/strict";
import test from "node:test";
import {
  focusedLaunchIdFromSearch,
  launchHistorySearch,
  shouldAutoOpenHistory,
} from "../src/lib/launchNavigation.ts";

test("only accepted launches automatically open History", () => {
  assert.equal(shouldAutoOpenHistory("ACCEPTED"), true);
  assert.equal(shouldAutoOpenHistory("accepted"), true);
  assert.equal(shouldAutoOpenHistory("SKIPPED"), false);
  assert.equal(shouldAutoOpenHistory("DEFINITIVE_FAILURE"), false);
  assert.equal(shouldAutoOpenHistory("SUBMISSION_UNKNOWN"), false);
});

test("round-trips a focused launch ID through the History query", () => {
  const launchId = "launch/a b";
  const search = launchHistorySearch(launchId);
  assert.equal(focusedLaunchIdFromSearch(search), launchId);
});

test("rejects an oversized focused launch ID", () => {
  assert.equal(focusedLaunchIdFromSearch(`?launchId=${"x".repeat(257)}`), "");
});
