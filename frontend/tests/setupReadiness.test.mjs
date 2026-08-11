import assert from "node:assert/strict";
import test from "node:test";
import { missingSetupRequirements } from "../src/lib/setupReadiness.ts";

test("requires both AgentLoop and a local Agent when neither exists", () => {
  assert.deepEqual(
    missingSetupRequirements({ agentLoopConfigured: false, localAgentCount: 0 }),
    ["agentloop", "local-agent"],
  );
});

test("only reports the missing setup item", () => {
  assert.deepEqual(
    missingSetupRequirements({ agentLoopConfigured: true, localAgentCount: 0 }),
    ["local-agent"],
  );
  assert.deepEqual(
    missingSetupRequirements({ agentLoopConfigured: false, localAgentCount: 1 }),
    ["agentloop"],
  );
});

test("does not remind after both setup items exist", () => {
  assert.deepEqual(
    missingSetupRequirements({ agentLoopConfigured: true, localAgentCount: 1 }),
    [],
  );
});

test("does not treat an unavailable readiness check as a missing configuration", () => {
  assert.deepEqual(
    missingSetupRequirements({ agentLoopConfigured: null, localAgentCount: null }),
    [],
  );
});
