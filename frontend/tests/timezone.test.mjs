import assert from "node:assert/strict";
import test from "node:test";
import {
  normalizeIanaTimezone,
  utcInstantToZonedLocalInput,
  zonedLocalDateTimeToUtc,
} from "../src/lib/timezone.ts";

test("converts a named-zone wall clock without using the host timezone", () => {
  assert.equal(zonedLocalDateTimeToUtc("2026-08-08T09:00", "Asia/Shanghai"), "2026-08-08T01:00:00.000Z");
  assert.equal(zonedLocalDateTimeToUtc("2026-01-15T09:00", "America/New_York"), "2026-01-15T14:00:00.000Z");
  assert.equal(utcInstantToZonedLocalInput("2026-08-08T01:00:00Z", "Asia/Shanghai"), "2026-08-08T09:00");
});

test("rejects nonexistent DST wall-clock time", () => {
  assert.throws(
    () => zonedLocalDateTimeToUtc("2026-03-08T02:30", "America/New_York"),
    /不存在/,
  );
});

test("rejects ambiguous DST wall-clock time", () => {
  assert.throws(
    () => zonedLocalDateTimeToUtc("2026-11-01T01:30", "America/New_York"),
    /歧义/,
  );
});

test("rejects invalid IANA timezone", () => {
  assert.throws(() => normalizeIanaTimezone("Mars/Olympus"), /IANA 时区/);
});
