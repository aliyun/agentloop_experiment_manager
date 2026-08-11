import assert from "node:assert/strict";
import test from "node:test";
import { formatCadence } from "../src/lib/format.ts";

test("formats the new minute cadence and keeps legacy hours readable", () => {
  assert.equal(
    formatCadence({ type: "INTERVAL_MINUTES", intervalMinutes: 5 }),
    "每 5 分钟",
  );
  assert.equal(
    formatCadence({ type: "INTERVAL_HOURS", intervalHours: 2 }),
    "每 2 小时",
  );
});
