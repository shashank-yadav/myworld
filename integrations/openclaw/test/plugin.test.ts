import { test } from "node:test";
import assert from "node:assert/strict";
import { register, OpenClawObserver, scrub, toolKind } from "../index.ts";

function fakeApi() {
  const handlers = new Map<string, (e: any, c: any) => unknown>();
  return { handlers, api: { on: (h: string, fn: any) => handlers.set(h, fn), pluginConfig: { endpoint: "http://collector.test" } } };
}

test("maps a run with an llm call and a timed-out tool to aops events", async () => {
  const sent: any[] = [];
  globalThis.fetch = (async (_url: string, init: any) => {
    sent.push(...JSON.parse(init.body).events);
    return new Response("{}", { status: 200 });
  }) as any;
  const { api, handlers } = fakeApi();
  const obs = register(api as any);
  const ctx = { sessionKey: "s1", agentId: "main" };
  const fire = (h: string, e: any) => handlers.get(h)!(e, ctx);

  fire("before_agent_start", { prompt: "Send the weekly update\nmore detail", runId: "r1" });
  fire("llm_input", { model: "claude-sonnet-5", provider: "anthropic", prompt: "hi" });
  fire("llm_output", { model: "claude-sonnet-5", usage: { input: 100, output: 20 }, assistantTexts: ["ok"] });
  fire("before_tool_call", { toolName: "gmail_send_email", params: { to: "a@b.c", apiKey: "sk-ant-abcdefghijklmnopqrstu" } });
  fire("after_tool_call", { toolName: "gmail_send_email", error: "ReadTimeout: timed out", durationMs: 30000 });
  fire("agent_end", { success: true });
  await obs.ship.flush();

  const types = sent.map((e) => e.type);
  assert.deepEqual(types, ["run.start", "span.start", "span.end", "span.start", "span.end", "run.end"]);
  assert.equal(sent[0].name, "Send the weekly update");
  assert.equal(sent[0].run_id, "oc-r1");
  const toolStart = sent[3], toolEnd = sent[4];
  assert.equal(toolStart.span_id, toolEnd.span_id);
  assert.equal(toolStart.parent_span_id, sent[1].span_id);
  assert.equal(toolStart.attrs.args.apiKey, "[REDACTED]");
  assert.equal(toolEnd.attrs.status, "timeout");
  assert.ok(sent.every((e) => e.source === "openclaw"));
});

test("handler errors never propagate into the agent", () => {
  const { api, handlers } = fakeApi();
  const obs = new OpenClawObserver("http://collector.test");
  obs.before_tool_call = () => { throw new Error("boom"); };
  register(api as any, obs);
  assert.doesNotThrow(() => handlers.get("before_tool_call")!({}, {}));
});

test("helpers", () => {
  assert.equal(toolKind("exec"), "terminal");
  assert.equal(toolKind("browser"), "browser");
  assert.equal(toolKind("github__create_issue"), "mcp");
  assert.equal(scrub("token ghp_abcdefghijklmnopqrstuvwxyz0123"), "token [REDACTED]");
  assert.equal(scrub("secret text", false), "<11 chars>");
});
