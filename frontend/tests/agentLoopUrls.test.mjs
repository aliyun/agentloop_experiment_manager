import assert from "node:assert/strict";
import test from "node:test";
import {
  normalizeAgentLoopApiEndpoint,
  normalizeAgentLoopConsoleBaseUrl,
} from "../src/lib/agentLoopUrls.ts";

test("accepts the SDK endpoint hostname with or without an HTTPS scheme", () => {
  assert.equal(
    normalizeAgentLoopApiEndpoint(" agentloop.cn-hangzhou.aliyuncs.com "),
    "agentloop.cn-hangzhou.aliyuncs.com",
  );
  assert.equal(
    normalizeAgentLoopApiEndpoint("https://agentloop.cn-hangzhou.aliyuncs.com/"),
    "agentloop.cn-hangzhou.aliyuncs.com",
  );
  assert.equal(normalizeAgentLoopApiEndpoint(""), null);
});

test("rejects an API endpoint path instead of silently storing a bad SDK endpoint", () => {
  assert.throws(
    () => normalizeAgentLoopApiEndpoint("agentloop.example.test/v1"),
    /只能填写域名和端口/,
  );
});

test("normalizes a console host or pasted detail URL to its HTTP origin", () => {
  assert.equal(
    normalizeAgentLoopConsoleBaseUrl("agentloop.console.aliyun.com"),
    "https://agentloop.console.aliyun.com",
  );
  assert.equal(
    normalizeAgentLoopConsoleBaseUrl(
      "https://agentloop.console.aliyun.com/agentloop/region/cn-hangzhou/agentspace/demo/app/trajectory?tab=all",
    ),
    "https://agentloop.console.aliyun.com",
  );
  assert.equal(normalizeAgentLoopConsoleBaseUrl("  "), null);
});

test("rejects unsupported schemes and embedded credentials", () => {
  assert.throws(() => normalizeAgentLoopConsoleBaseUrl("ftp://example.test"), /只支持 HTTP/);
  assert.throws(
    () => normalizeAgentLoopConsoleBaseUrl("https://user:secret@example.test"),
    /不能包含用户名或密码/,
  );
});
