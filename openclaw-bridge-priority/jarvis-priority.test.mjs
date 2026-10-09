// Run: node --test openclaw-bridge-priority/jarvis-priority.test.mjs   (Node standard library only)
// The plugin is pointed (JARVIS_BRIDGE_SETTINGS) at a temp copy of the live
// bridge.config.json, so the tests use production values and may edit the copy.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const LIVE_CONFIG = fileURLToPath(new URL("../bridge.config.json", import.meta.url));
const LIVE_TEXT = fs.readFileSync(LIVE_CONFIG, "utf8").replace(/^\uFEFF/, "");
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "jbp-"));
const TEST_CONFIG = path.join(tmpDir, "bridge.config.json");
function writeConfig(mutate) {
  const cfg = JSON.parse(LIVE_TEXT);
  if (mutate) mutate(cfg);
  fs.writeFileSync(TEST_CONFIG, "\uFEFF" + JSON.stringify(cfg, null, 2));
}
writeConfig();
process.env.JARVIS_BRIDGE_SETTINGS = TEST_CONFIG;
const {
  default: plugin, createBridgePriority, createState, loadSettings, resolveSettings, describeSettings, shortJobCaps, PRIORITY_KEYS, SHARED_KEYS,
} = await import("./jarvis-priority.js");
test.after(() => fs.rmSync(tmpDir, { recursive: true, force: true }));

const LIVE = loadSettings(LIVE_CONFIG);
const S = LIVE.settings;
const STATE_KEY = Symbol.for(`${plugin.id}.state.v2`);
const quietLogger = (sink = []) => ({ info: (m) => sink.push(m), warn: (m) => sink.push(m), error: (m) => sink.push(m) });

const BRIDGE_PROMPT = "You are Neo ... Task package to process now: X:\\inbox\\t-001.jarvis-task.json";
const bridgeCtx = (over = {}) => ({ runId: "cron-run-1", sessionKey: "agent:main:cron:job-1:run:a", trigger: "cron", modelProviderId: "ollama", ...over });
const chatCtx = (over = {}) => ({
  runId: "chat-run-1", sessionId: "sess-42", sessionKey: "agent:main:telegram:direct:42", trigger: "user", modelProviderId: "ollama",
  messageProvider: "telegram", chatId: "42", ...over,
});

const MIN = 60_000;

// run.alive: what the gateway's active-run registry says about the bridge run
// (true / false / undefined = cannot tell). run.task: the task package JSON.
// overrides: setting names (see PRIORITY_KEYS / SHARED_KEYS) replacing the live values.
function harness(overrides = {}, state = createState(), toolHoldMs) {
  const clock = { t: 0 };
  const calls = { abort: [], notify: [], logs: [], reads: [] };
  const run = { alive: undefined, task: '{"task_id":"t-001","timeout_minutes":60}' };
  const deps = {
    now: () => clock.t,
    sleep: async (ms) => { clock.t += ms; await new Promise((r) => setImmediate(r)); },
    log: (level, msg) => calls.logs.push(`${level}: ${msg}`),
    abortRun: async (chat) => { calls.abort.push(chat); return true; },
    notify: async (chat, text) => { calls.notify.push({ target: `${chat.channel}:${chat.chatId}`, text }); },
    readFile: (file) => {
      calls.reads.push(file);
      if (run.task === null) throw new Error("ENOENT");
      return run.task;
    },
    isRunActive: () => run.alive,
  };
  const settings = { ...S, ...overrides };
  const p = createBridgePriority(settings, deps, state, toolHoldMs);
  return { p, clock, calls, run };
}

const tick = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
};

test("the live bridge.config.json supplies every setting", () => {
  assert.deepEqual(LIVE.problems, []);
  const names = Object.values({ ...PRIORITY_KEYS, ...SHARED_KEYS }).map(([n]) => n);
  assert.equal(Object.keys(PRIORITY_KEYS).length, 14);
  assert.deepEqual(Object.keys(S).sort(), names.sort());
  assert.ok(Object.keys(PRIORITY_KEYS).every((k) => k.startsWith("priority.")));
});

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

test("there is no separate hold timer setting: not in the schema (unknown keys are rejected), not in resolved settings", () => {
  const schema = JSON.parse(fs.readFileSync(new URL("./openclaw.plugin.json", import.meta.url), "utf8")).configSchema;
  assert.equal(schema.additionalProperties, false);
  const timerKey = (k) => /hold.*minutes/i.test(k);
  assert.deepEqual(Object.keys(schema.properties).filter(timerKey), []);
  assert.deepEqual(Object.keys(S).filter(timerKey), []);
  assert.deepEqual(Object.keys(JSON.parse(LIVE_TEXT).priority).filter(timerKey), []);
});

test("a chat turn waits for the bridge task's whole lifetime, however long, and is released when it ends", async () => {
  const { p, clock, calls, run } = harness();
  run.alive = true;
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  let released = false;
  const held = p.beforeModelResolve({ prompt: "hi" }, chatCtx()).then(() => { released = true; });
  while (clock.t < 45 * MIN) await new Promise((r) => setImmediate(r));
  assert.equal(released, false, "still held at 45 min (old timers were 2 / 12 / 35 min)");
  assert.equal(p.bridges.size, 1);
  await p.agentEnd({ success: true }, bridgeCtx());
  await held;
  assert.equal(released, true);
  assert.ok(clock.t < 46 * MIN, "released promptly when the task ended");
  assert.equal(calls.logs.some((l) => l.includes("letting it run alongside")), false);
});

test("timeout_minutes is read from the task package named in the bridge prompt", async () => {
  const { p, calls, run } = harness();
  run.task = '\uFEFF{"task_id":"t-001","timeout_minutes":"25"}';
  const prompt = "You are Neo ... waiting in C:\\Users\\example\\openclaw-inbox (exact path at the end). Task package to process now: C:\\Users\\example\\openclaw-inbox\\t-001.jarvis-task.json";
  await p.beforeModelResolve({ prompt }, bridgeCtx());
  assert.deepEqual(calls.reads, ["C:\\Users\\example\\openclaw-inbox\\t-001.jarvis-task.json"]);
  assert.equal(p.bridges.get("cron-run-1").timeoutMinutes, 25);
  assert.ok(calls.logs.some((l) => l.includes("bridge run started: cron-run-1") && l.includes("timeout_minutes 25")));
});

test("a bridge run with no end event releases chat exactly when its task's timeout_minutes fires", async () => {
  const { p, clock, calls, run } = harness();
  run.task = '{"timeout_minutes":10}';
  run.alive = true;
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  await p.beforeModelResolve({ prompt: "long question" }, chatCtx({ modelProviderId: "flm" }));
  let released = false;
  const held = p.beforeModelResolve({ prompt: "hi" }, chatCtx({ runId: "chat-run-2" })).then(() => { released = true; });
  while (clock.t < 9 * MIN + 50_000) await new Promise((r) => setImmediate(r));
  assert.equal(released, false, "held before timeout_minutes");
  await held;
  assert.ok(clock.t >= 10 * MIN && clock.t <= 10 * MIN + S.bridgeWaitPollMs, `released at ${clock.t} ms`);
  assert.equal(p.bridges.size, 0);
  assert.ok(calls.logs.some((l) => l.startsWith("warn: bridge run finished: cron-run-1") && l.includes("timeout_minutes (10) reached")));
});

// --- per-task run limit (bridge watcher cap fix) ---
const bridgeAborts = (calls) => calls.abort.filter((a) => a.sessionKey === bridgeCtx().sessionKey);

async function startTask(timeoutMinutes, over = {}) {
  const h = harness(over);
  h.run.task = timeoutMinutes === null ? null : JSON.stringify({ task_id: "t-001", timeout_minutes: timeoutMinutes });
  h.run.alive = true;
  await h.p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx({ sessionId: "bridge-sess-1" }));
  return h;
}

test("a timeout_minutes 90 task is NOT stopped at 900 s (15 min) and IS stopped when its 90 minutes are up", async () => {
  const { p, clock, calls } = await startTask(90);
  assert.equal(p.bridges.get("cron-run-1").timeoutMinutes, 90);
  for (const at of [15 * MIN, 15 * MIN + 1, 45 * MIN, 90 * MIN - 1]) {
    clock.t = at;
    await p.stopOverdue();
    assert.equal(bridgeAborts(calls).length, 0, `not stopped at ${at / 1000} s`);
    assert.equal(p.bridges.size, 1, `still running at ${at / 1000} s`);
  }
  clock.t = 90 * MIN;
  await p.stopOverdue();
  assert.equal(bridgeAborts(calls).length, 1, "stopped at 5400 s");
  assert.equal(bridgeAborts(calls)[0].sessionId, "bridge-sess-1", "stopped by its own session id");
  assert.equal(p.bridges.size, 0);
  assert.ok(calls.logs.some((l) => l.startsWith("warn: bridge run finished: cron-run-1 after 90.0 min") && l.includes("timeout_minutes (90) reached; run stopped")));
});

test("a timeout_minutes 15 task is still stopped at 15 minutes", async () => {
  const { p, clock, calls } = await startTask(15);
  clock.t = 15 * MIN - 1;
  await p.stopOverdue();
  assert.equal(bridgeAborts(calls).length, 0, "not stopped at 899.999 s");
  clock.t = 15 * MIN;
  await p.stopOverdue();
  assert.equal(bridgeAborts(calls).length, 1, "stopped at 900 s");
  assert.ok(calls.logs.some((l) => l.includes("bridge run finished: cron-run-1 after 15.0 min") && l.includes("timeout_minutes (15) reached; run stopped")));
});

test("timeout_minutes above the contract maximum is capped at 720 min; an unreadable one uses 720 min", async () => {
  for (const tm of [5000, null]) {
    const { p, clock, calls } = await startTask(tm, { taskTimeoutMaxMinutes: 720 });
    clock.t = 720 * MIN - 1;
    await p.stopOverdue();
    assert.equal(bridgeAborts(calls).length, 0, `${tm}: not stopped before 720 min`);
    clock.t = 720 * MIN;
    await p.stopOverdue();
    assert.equal(bridgeAborts(calls).length, 1, `${tm}: stopped at 720 min`);
    assert.ok(calls.logs.some((l) => l.includes("timeout_minutes (720) reached")));
  }
});

test("overlapping checks stop an overdue run once; a run already gone is reported as not stopped", async () => {
  const { p, clock, calls } = await startTask(30);
  clock.t = 30 * MIN;
  await Promise.all([p.stopOverdue(), p.stopOverdue(), p.beforeModelResolve({ prompt: "hi" }, chatCtx({ modelProviderId: "flm" }))]);
  assert.equal(bridgeAborts(calls).length, 1);
  const h2 = harness();
  h2.run.task = '{"timeout_minutes":1}';
  const failingAbort = createBridgePriority(S, {
    now: () => h2.clock.t, sleep: async () => {}, log: (lv, m) => h2.calls.logs.push(`${lv}: ${m}`),
    abortRun: async () => false, notify: async () => {}, readFile: () => h2.run.task, isRunActive: () => undefined,
  });
  await failingAbort.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  h2.clock.t = MIN;
  await failingAbort.stopOverdue();
  assert.equal(failingAbort.bridges.size, 0, "the window still ends");
  assert.ok(h2.calls.logs.some((l) => l.includes("timeout_minutes (1) reached; run not stopped")));
});

test("the plugin's own timer stops an overdue run with no chat activity (real timers)", async () => {
  delete globalThis[STATE_KEY];
  const taskFile = path.join(tmpDir, "t-timer.jarvis-task.json");
  fs.writeFileSync(taskFile, JSON.stringify({ task_id: "t-timer", timeout_minutes: 0.005 })); // 300 ms
  const logs = [];
  const hooks = {};
  try {
    writeConfig((c) => { c.priority.bridge_wait_poll_ms = 20; });
    plugin.register({ pluginConfig: {}, logger: quietLogger(logs), on: (name, fn) => (hooks[name] = fn) });
    const timer = globalThis[STATE_KEY].timeoutTimer;
    assert.ok(timer, "timer started at register");
    await hooks.before_model_resolve({ prompt: `Task package to process now: ${taskFile}` }, bridgeCtx({ runId: "timer-run" }));
    await new Promise((r) => setTimeout(r, 100));
    assert.equal(logs.some((l) => l.includes("bridge run finished: timer-run")), false, "not before its timeout");
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline && !logs.some((l) => l.includes("bridge run finished: timer-run"))) await new Promise((r) => setTimeout(r, 20));
    assert.ok(logs.some((l) => l.includes("bridge run finished: timer-run") && l.includes("timeout_minutes (0.005) reached")), logs.join("\n"));
    plugin.register({ pluginConfig: {}, logger: quietLogger(), on() {} });
    assert.notEqual(globalThis[STATE_KEY].timeoutTimer, timer, "re-register replaces the timer");
  } finally {
    writeConfig();
    clearInterval(globalThis[STATE_KEY].timeoutTimer);
  }
});

test("a watcher automation capped below the contract maximum is flagged; the ceiling is not", () => {
  const names = ["jarvis-bridge-inbox-watcher"];
  const job = (t) => ({ id: "j1", name: names[0], payload: { kind: "agentTurn", ...(t === undefined ? {} : { timeoutSeconds: t }) } });
  assert.deepEqual(shortJobCaps([job(900)], names, 720), ["jarvis-bridge-inbox-watcher (j1) timeoutSeconds=900"]);
  assert.deepEqual(shortJobCaps([job(undefined)], names, 720), ["jarvis-bridge-inbox-watcher (j1) timeoutSeconds=unset"]);
  assert.deepEqual(shortJobCaps([job(720 * 60)], names, 720), []);
  assert.deepEqual(shortJobCaps([job(0)], names, 720), [], "0 = no OpenClaw limit; the plugin enforces the ceiling");
  assert.deepEqual(shortJobCaps([{ ...job(900), name: "other" }], names, 720), []);
});

test("an unreadable task timeout falls back to the contract maximum, not a short hold", async () => {
  const { p, clock, run } = harness({ taskTimeoutMaxMinutes: 720 });
  run.task = null;
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  clock.t = 700 * MIN;
  await p.beforeModelResolve({ prompt: "hi" }, chatCtx({ modelProviderId: "flm" }));
  assert.equal(p.bridges.size, 1, "still held at 700 min");
  clock.t = 721 * MIN;
  await p.beforeModelResolve({ prompt: "hi" }, chatCtx({ runId: "chat-run-3" }));
  assert.equal(p.bridges.size, 0, "released at the 720-min contract maximum");
});

test("a bridge run that dies (gone from the gateway past the heartbeat stale limit) releases chat", async () => {
  const { p, clock, calls, run } = harness({ workerStaleSeconds: 300 });
  run.alive = true;
  await p.beforeModelResolve({ prompt: "long question" }, chatCtx());
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  clock.t = 3 * MIN;
  await p.modelCallStarted({ provider: "ollama" }, bridgeCtx());
  run.alive = false; // the run vanished without agent_end (e.g. a gateway restart)
  let released = false;
  const held = p.beforeModelResolve({ prompt: "hi" }, chatCtx({ runId: "chat-run-2" })).then(() => { released = true; });
  while (clock.t < 3 * MIN + 290_000) await new Promise((r) => setImmediate(r));
  assert.equal(released, false, "not declared dead before the 300 s stale limit");
  await held;
  assert.ok(clock.t > 8 * MIN && clock.t <= 8 * MIN + S.bridgeWaitPollMs, `released at ${clock.t} ms`);
  assert.ok(calls.logs.some((l) => l.includes("bridge run finished: cron-run-1") && l.includes("worker dead")));
  assert.match(calls.notify.at(-1).text, /finished/, "a stopped chat is told the model is free");
});

test("a bridge run never seen in the registry is not declared dead; timeout_minutes still bounds it", async () => {
  const { p, clock, run } = harness();
  run.task = '{"timeout_minutes":30}';
  run.alive = false;
  await p.beforeModelResolve({ prompt: BRIDGE_PROMPT }, bridgeCtx());
  clock.t = 20 * MIN;
  await p.beforeModelResolve({ prompt: "hi" }, chatCtx({ modelProviderId: "flm" }));
  await p.beforeToolCall({}, { runId: "chat-run-1", sessionKey: chatCtx().sessionKey }); // flm chat: not paused
  assert.equal(p.bridges.size, 1, "no liveness signal is not proof of death");
  clock.t = 31 * MIN;
  await p.beforeToolCall({}, { runId: "chat-run-1", sessionKey: chatCtx().sessionKey });
  assert.equal(p.bridges.size, 0);
});

test("each setting follows its bridge.config.json key", () => {
  const cfg = JSON.parse(LIVE_TEXT);
  for (const [key, [name, kind]] of Object.entries({ ...PRIORITY_KEYS, ...SHARED_KEYS })) {
    const copy = JSON.parse(JSON.stringify(cfg));
    const parts = key.split(".");
    const parent = parts.slice(0, -1).reduce((o, k) => o[k], copy);
    const changed = { string: "changed-value", strings: ["changed-a", "changed-b"], boolean: !S[name], positive: S[name] + 7, nonNegative: S[name] + 7, integer: S[name] + 7 }[kind];
    parent[parts.at(-1)] = changed;
    const r = resolveSettings(copy);
    assert.deepEqual(r.problems, [], key);
    assert.deepEqual(r.settings[name], changed, key);
  }
});

test("a missing or mistyped key is reported by name; an unreadable file is reported", () => {
  const cfg = JSON.parse(LIVE_TEXT);
  delete cfg.priority.hold_chat;
  cfg.priority.bridge_wait_poll_ms = "2000";
  cfg.timeouts.task_timeout_max_minutes = 0;
  assert.deepEqual(resolveSettings(cfg).problems, [
    "priority.hold_chat (missing)",
    "priority.bridge_wait_poll_ms (not positive)",
    "timeouts.task_timeout_max_minutes (not positive)",
  ]);
  assert.match(loadSettings(path.join(tmpDir, "missing.json")).problems[0], /cannot read/);
});

test("the task package path pattern comes from executors.task_file_suffix", async () => {
  const { p, calls } = harness({ taskFileSuffix: ".other-task.json" });
  await p.beforeModelResolve({ prompt: "Task package to process now: C:\\in\\t-9.other-task.json" }, bridgeCtx({ jobId: "j" }));
  assert.deepEqual(calls.reads, []);
  p.setBridgeJobs([{ id: "j", name: S.bridgeJobNames[0] }], S.bridgeJobNames);
  await p.beforeModelResolve({ prompt: "Task package to process now: C:\\in\\t-9.other-task.json" }, bridgeCtx({ runId: "r2", jobId: "j" }));
  assert.deepEqual(calls.reads, ["C:\\in\\t-9.other-task.json"]);
});

test("register reads bridge.config.json each time: a changed value is used, a broken file leaves the plugin inactive", () => {
  const run = () => {
    delete globalThis[STATE_KEY];
    const logs = [];
    const hooks = {};
    plugin.register({ pluginConfig: {}, logger: quietLogger(logs), on: (name, _fn, opts) => (hooks[name] = opts || {}) });
    return { logs, hooks };
  };
  try {
    writeConfig((c) => { c.priority.bridge_wait_poll_ms = 1500; c.priority.hook_priority = 55; });
    let r = run();
    assert.ok(r.logs.some((l) => l.includes("priority.bridge_wait_poll_ms=1500")), "changed value is logged");
    assert.equal(r.hooks.before_model_resolve.priority, 55);
    writeConfig((c) => { delete c.priority.notify_chat; });
    r = run();
    assert.deepEqual(r.hooks, {}, "no hooks when a key is missing");
    assert.ok(r.logs.some((l) => l.includes("NOT ACTIVE") && l.includes("priority.notify_chat (missing)")));
    writeConfig((c) => { c.priority.plugin_id = "someone-else"; });
    r = run();
    assert.deepEqual(r.hooks, {});
    assert.ok(r.logs.some((l) => l.includes("NOT ACTIVE") && l.includes("someone-else")));
  } finally {
    writeConfig();
  }
  const r = run();
  assert.ok(r.logs.some((l) => l.includes(describeSettings(S))), "back to the live values");
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
  plugin.register({ pluginConfig: {}, logger: quietLogger(), on: (name, _fn, opts) => (hooks[name] = opts || {}) });
  for (const h of ["before_model_resolve", "model_call_started", "before_tool_call", "agent_end", "cron_reconciled", "cron_changed"]) assert.ok(h in hooks, h);
  assert.equal(hooks.before_tool_call.timeoutMs, S.beforeToolCallHookMs, "tool pause needs the maximum hook budget");
  assert.equal(hooks.before_tool_call.priority, S.hookPriority);
  assert.equal(hooks.before_model_resolve.priority, S.hookPriority);
  assert.equal(plugin.id, S.pluginId);
});

test("settings left in openclaw.json are ignored, with a warning", () => {
  delete globalThis[STATE_KEY];
  const logs = [];
  const hooks = {};
  plugin.register({ pluginConfig: { holdChat: false }, logger: quietLogger(logs), on: (name, fn) => (hooks[name] = fn) });
  assert.ok(logs.some((l) => l.includes("holdChat") && l.includes("is ignored")));
  assert.ok(logs.some((l) => l.includes(`hold chat=${S.holdChat}`)), "the bridge.config.json value wins");
});

test("tool pause is bounded by the operator's before_tool_call hook budget", () => {
  delete globalThis[STATE_KEY];
  const logs = [];
  const config = { plugins: { entries: { [S.pluginId]: { hooks: { timeouts: { before_tool_call: 15000 } } } } } };
  plugin.register({ pluginConfig: {}, config, logger: quietLogger(logs), on() {} });
  assert.ok(logs.some((l) => l.includes("up to 0 s")), "a 15 s budget leaves no room to pause; the run is stopped instead");
});

test("chat notices go through the channel's outbound adapter; channels without one are skipped", async () => {
  delete globalThis[STATE_KEY];
  const hooks = {};
  const sent = [];
  const logs = [];
  plugin.register({
    pluginConfig: {},
    logger: quietLogger(logs),
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
