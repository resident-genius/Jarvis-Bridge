// Run: node --test openclaw-bridge-priority/bridge-priority.test.mjs   (Node standard library only)
import test from "node:test";
import assert from "node:assert/strict";
import plugin, { createBridgePriority, createState, resolveSettings } from "./bridge-priority.js";

const STATE_KEY = Symbol.for("jarvis-bridge-priority.state.v2");

const BRIDGE_PROMPT = "You are Neo ... Task package to process now: X:\\inbox\\t-001.jarvis-task.json";
const bridgeCtx = (over = {}) => ({ runId: "cron-run-1", sessionKey: "agent:main:cron:job-1:run:a", trigger: "cron", modelProviderId: "ollama", ...over });
const chatCtx = (over = {}) => ({
  runId: "chat-run-1", sessionId: "sess-42", sessionKey: "agent:main:telegram:direct:42", trigger: "user", modelProviderId: "ollama",
  messageProvider: "telegram", chatId: "42", ...over,
});

function harness(overrides = {}, state = createState(), toolHoldMs) {
  const clock = { t: 0 };
  const calls = { abort: [], notify: [], logs: [] };
  const deps = {
    now: () => clock.t,
    sleep: async (ms) => { clock.t += ms; await new Promise((r) => setImmediate(r)); },
    log: (level, msg) => calls.logs.push(`${level}: ${msg}`),
    abortRun: async (chat) => { calls.abort.push(chat); return true; },
    notify: async (chat, text) => { calls.notify.push({ target: `${chat.channel}:${chat.chatId}`, text }); },
  };
  const settings = resolveSettings(overrides);
  const p = createBridgePriority(settings, deps, state, toolHoldMs ?? settings.maxToolHoldMinutes * 60_000);
  return { p, clock, calls };
}

const tick = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
};

test("bridge runs are recognised; chat and unrelated cron runs are not", () => {
  const { p } = harness();
  assert.equal(p.isBridge({ prompt: BRIDGE_PROMPT }, bridgeCtx()), true);
  assert.equal(p.isBridge({ prompt: BRIDGE_PROMPT }, chatCtx()), false, "a chat message quoting a task file is still chat");
  assert.equal(p.isBridge({ prompt: "daily research slice" }, bridgeCtx()), false);
  assert.equal(p.isBridge({ prompt: BRIDGE_PROMPT }, bridgeCtx({ trigger: "heartbeat" })), false);
  p.setBridgeJobs([{ id: "job-1", name: "jarvis-bridge-inbox-watcher" }, { id: "job-2", name: "other" }], ["jarvis-bridge-inbox-watcher"]);
  assert.equal(p.isBridge({ prompt: "" }, bridgeCtx({ jobId: "job-1" })), true);
  assert.equal(p.isBridge({ prompt: "" }, bridgeCtx({ jobId: undefined })), true, "matched by session key");
  assert.equal(p.isBridge({ prompt: "" }, bridgeCtx({ jobId: "job-2", sessionKey: "agent:main:cron:job-2" })), false);
});

test("a chat turn arriving during a bridge run waits until the bridge run ends", async () => {
  const { p, clock, calls } = harness();
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  let released = false;
  const held = p.beforeModelResolve({ prompt: "hi" }, chatCtx()).then(() => { released = true; });
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.equal(released, false, "chat must wait while the bridge run is active");
  clock.t += 60_000;
  await p.agentEnd({ success: true }, bridgeCtx());
  await held;
  assert.equal(released, true);
  assert.equal(calls.notify.length, 1);
  assert.match(calls.notify[0].text, /queued/);
  assert.equal(p.chats.size, 1, "released chat run is tracked as in flight");
});

test("chat on a different provider (NPU) is not held", async () => {
  const { p, calls } = harness();
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  await p.beforeModelResolve({ prompt: "hi" }, chatCtx({ modelProviderId: "flm" }));
  assert.equal(calls.notify.length, 0);
});

test("an in-flight chat run is stopped when a bridge run starts, and told when it is over", async () => {
  const { p, calls } = harness();
  await p.beforeModelResolve({ prompt: "long question" }, chatCtx());
  assert.equal(p.chats.size, 1);
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  assert.equal(calls.abort.length, 1);
  assert.equal(calls.abort[0].sessionKey, "agent:main:telegram:direct:42");
  assert.equal(calls.abort[0].runId, "chat-run-1");
  assert.equal(calls.abort[0].sessionId, "sess-42", "the in-process abort needs the session id");
  assert.match(calls.notify[0].text, /Paused/);
  await p.agentEnd({ success: true }, bridgeCtx());
  assert.match(calls.notify.at(-1).text, /finished/);
});

test("preemption and holding can be switched off", async () => {
  const { p, calls } = harness({ preemptInFlightChat: false, holdChat: false });
  await p.beforeModelResolve({ prompt: "q" }, chatCtx());
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  await p.beforeModelResolve({ prompt: "q2" }, chatCtx({ runId: "chat-run-2" }));
  assert.equal(calls.abort.length, 0);
  assert.equal(calls.notify.length, 0);
});

test("a bridge run that never reports its end stops blocking after maxBridgeHoldMinutes", async () => {
  const { p, clock } = harness({ maxBridgeHoldMinutes: 5 });
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  clock.t += 6 * 60_000;
  await p.beforeModelResolve({ prompt: "hi" }, chatCtx());
  assert.equal(p.bridges.size, 0);
});

test("a chat turn never waits longer than maxChatHoldMinutes", async () => {
  const { p, clock, calls } = harness({ maxChatHoldMinutes: 2, maxBridgeHoldMinutes: 60 });
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  await p.beforeModelResolve({ prompt: "hi" }, chatCtx());
  assert.ok(clock.t >= 2 * 60_000 && clock.t < 3 * 60_000);
  assert.ok(calls.logs.some((l) => l.startsWith("warn:") && l.includes("waited 2 min")));
});

test("an in-flight chat run survives a plugin hot reload and is still stopped", async () => {
  const state = createState();
  const before = harness({}, state);
  await before.p.beforeModelResolve({ prompt: "long browser task" }, chatCtx());
  const after = harness({}, state); // new plugin instance, same process-wide state
  await after.p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  assert.equal(after.calls.abort.length, 1);
  assert.equal(after.calls.abort[0].runId, "chat-run-1");
});

test("a running chat pauses at its next tool call and resumes when the bridge run ends", async () => {
  const { p, clock, calls } = harness({ preemptInFlightChat: false });
  await p.beforeModelResolve({ prompt: "multi-step task" }, chatCtx());
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  let result = "pending";
  const tool = p.beforeToolCall({ toolName: "browser" }, { runId: "chat-run-1", sessionId: "sess-42", sessionKey: chatCtx().sessionKey }).then((r) => (result = r));
  await tick();
  assert.equal(result, "pending", "tool call must wait while the bridge run is active");
  clock.t += 30_000;
  await p.agentEnd({ success: true }, bridgeCtx());
  await tool;
  assert.equal(result, undefined, "tool call continues unchanged");
  assert.ok(calls.logs.some((l) => l.includes("resumed chat run chat-run-1")));
});

test("a tool pause that reaches its limit stops the chat run and blocks the tool", async () => {
  const { p, calls } = harness({}, createState(), 5 * 60_000);
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  // chat run that started before this plugin instance knew about it, found by session key
  p.state.chatSessionKeys.add(chatCtx().sessionKey);
  const r = await p.beforeToolCall({ toolName: "exec" }, { runId: "late-run", sessionId: "sess-9", sessionKey: chatCtx().sessionKey });
  assert.equal(r.block, true);
  assert.equal(calls.abort.at(-1).sessionId, "sess-9");
  assert.equal(calls.notify.at(-1).target, "telegram:42", "chat id comes from the direct-chat session key");
});

test("tool calls of bridge runs and unknown runs are never paused", async () => {
  const { p } = harness();
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  assert.equal(await p.beforeToolCall({ toolName: "exec" }, { runId: "cron-run-1", sessionKey: bridgeCtx().sessionKey }), undefined);
  assert.equal(await p.beforeToolCall({ toolName: "exec" }, { runId: "x", sessionKey: "agent:main:heartbeat" }), undefined);
});

test("a chat model call that starts on the bridge's provider during a bridge run is stopped; NPU calls are not", async () => {
  const { p, calls } = harness();
  await p.beforeModelResolve({ prompt: "q" }, chatCtx({ runId: "npu-run", modelProviderId: "flm" }));
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  assert.equal(calls.abort.length, 0, "NPU chat is not stopped when the bridge starts");
  await p.modelCallStarted({ provider: "flm" }, { runId: "npu-run", sessionId: "s-npu", sessionKey: chatCtx().sessionKey, modelProviderId: "flm" });
  assert.equal(calls.abort.length, 0, "NPU model call is not stopped");
  await p.modelCallStarted({ provider: "ollama" }, { runId: "npu-run", sessionId: "s-npu", sessionKey: chatCtx().sessionKey, modelProviderId: "ollama" });
  assert.equal(calls.abort.length, 1, "the same chat switching to Qwen is stopped");
});

test("a bridge run first seen at a model call (after a reload) still counts", async () => {
  const { p } = harness();
  p.setBridgeJobs([{ id: "job-1", name: "jarvis-bridge-inbox-watcher" }], ["jarvis-bridge-inbox-watcher"]);
  await p.modelCallStarted({ provider: "ollama" }, { runId: "r-9", sessionKey: "agent:main:cron:job-1:run:r-9", modelProviderId: "ollama" });
  assert.equal(p.bridges.size, 1);
  await p.cronFinished({ id: "job-1", state: { lastRunStatus: "error" } });
  assert.equal(p.bridges.size, 0, "automation finish ends the bridge window even without agent_end");
});

test("plugin registers the hooks it relies on", () => {
  delete globalThis[STATE_KEY];
  const hooks = {};
  plugin.register({ pluginConfig: {}, logger: { info() {}, warn() {} }, on: (name, _fn, opts) => (hooks[name] = opts || {}) });
  for (const h of ["before_model_resolve", "model_call_started", "before_tool_call", "agent_end", "cron_reconciled", "cron_changed"]) assert.ok(h in hooks, h);
  assert.equal(hooks.before_tool_call.timeoutMs, 600000, "tool pause needs the maximum hook budget");
});

test("tool pause is bounded by the operator's before_tool_call hook budget", () => {
  delete globalThis[STATE_KEY];
  const logs = [];
  const config = { plugins: { entries: { "jarvis-bridge-priority": { hooks: { timeouts: { before_tool_call: 15000 } } } } } };
  plugin.register({ pluginConfig: {}, config, logger: { info: (m) => logs.push(m), warn() {} }, on() {} });
  assert.ok(logs.some((l) => l.includes("up to 0 s")), "a 15 s budget leaves no room to pause; the run is stopped instead");
});

test("chat notices go through the channel's outbound adapter; channels without one are skipped", async () => {
  delete globalThis[STATE_KEY];
  const hooks = {};
  const sent = [];
  const logs = [];
  plugin.register({
    pluginConfig: {},
    logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m) },
    on: (name, fn) => (hooks[name] = fn),
    runtime: {
      config: { current: () => ({}) },
      channel: { outbound: { loadAdapter: async (id) => (id === "telegram" ? { sendText: async (c) => sent.push(c) } : undefined) } },
    },
  });
  await hooks.before_model_resolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  const held = hooks.before_model_resolve({ prompt: "hi" }, chatCtx());
  const heldWeb = hooks.before_model_resolve({ prompt: "hi" }, chatCtx({ runId: "web-1", messageProvider: "webchat", chatId: "webchat" }));
  await new Promise((r) => setTimeout(r, 20));
  await hooks.agent_end({ success: true }, bridgeCtx());
  await Promise.all([held, heldWeb]);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, "42");
  assert.equal(sent[0].silent, true);
  assert.match(sent[0].text, /queued/);
  assert.ok(logs.some((l) => l.includes("no outbound delivery for channel webchat")));
});
