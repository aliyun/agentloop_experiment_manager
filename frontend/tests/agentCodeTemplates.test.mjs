import assert from "node:assert/strict";
import test from "node:test";
import {
  AGENT_CODE_TEMPLATES,
  agentCodeTemplate,
  matchingAgentCodeTemplate,
} from "../src/lib/agentCodeTemplates.ts";

test("offers session HTTP, single HTTP, and echo templates", () => {
  assert.deepEqual(
    AGENT_CODE_TEMPLATES.map((template) => template.id),
    ["session-http", "single-http", "echo"],
  );
  const recommended = agentCodeTemplate("session-http");
  assert.ok(recommended);
  assert.match(recommended.source, /\/v1\/sessions/);
  assert.match(recommended.source, /\/messages/);
  assert.match(recommended.source, /async def solution_http/);
});

test("recognizes untouched templates and treats edits as custom", () => {
  const template = AGENT_CODE_TEMPLATES[1];
  assert.equal(matchingAgentCodeTemplate(template.source), template.id);
  assert.equal(matchingAgentCodeTemplate(`${template.source}\n# customer edit`), "custom");
});
