// Jarvis Bridge priority -- OpenClaw plugin (Node standard library only).
//
// Bridge task runs (the watcher automation named in bridge.config.json
// priority.bridge_job_names) and {{OPERATOR_NAME}}'s interactive chat share one local model. The model server answers one request
// at a time, and OpenClaw queues cron runs as "background" work, so a chat that
// keeps talking can starve a bridge run until it times out. This plugin flips
// that order:
//   * bridge run starts   -> every chat run using the same model provider is
//                            stopped (in-process run abort) and the chat is told why;
//   * while it is active  -> new chat turns wait in before_model_resolve, a chat
//                            run that reaches a tool call waits in before_tool_call,
//                            and a chat model call that still starts is stopped;
//   * bridge run ends     -> held chat turns and tool calls continue on their own.
// Chat is held for exactly the bridge task's lifetime. There is no separate hold
// timer: a bridge run that never reports its end is released when its task's
// timeout_minutes is reached, or when the run has dropped out of the gateway's
// active-run registry for longer than the runner's heartbeat stale limit.
// A bridge run's time limit is its own task's timeout_minutes, capped at
// timeouts.task_timeout_max_minutes (the contract ceiling, also used when the
// task's value cannot be read): once it passes, the plugin stops the run
// in-process. The check runs every priority.bridge_wait_poll_ms. The watcher
// automation's own timeoutSeconds must be the ceiling (max minutes * 60), not a
// shorter fixed cap; a shorter or missing one is logged as a warning.
// State lives on globalThis so a plugin hot reload does not forget in-flight runs.
// Every setting comes from bridge.config.json (section "priority", plus two shared
// timeouts and the task file suffix), read when the plugin registers. There are no
// built-in defaults: if a value is missing or has the wrong type the plugin logs
// which keys are wrong and registers no hooks, so OpenClaw runs as if it were off.
// The gateway imports a fresh temporary copy of this folder whenever it replaces
// the plugin, so code and settings changes both load when this plugin's entry in
// openclaw.json changes (or on an OpenClawGateway service restart). A load path
// edit that still names the same folder does not count as a change.
// Documentation: BRIDGE-PRIORITY.md next to this folder.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const VERSION = "3.2.0";
const MINUTE = 60 * 1000;
const BRIDGE_CONFIG_FILE = "bridge.config.json";
const PLUGIN_MANIFEST_FILE = "openclaw.plugin.json";

// bridge.config.json key -> [setting name, value check].
export const PRIORITY_KEYS = {
  "priority.plugin_id": ["pluginId", "string"],
  "priority.plugin_dir": ["pluginDir", "string"],
  "priority.bridge_job_names": ["bridgeJobNames", "strings"],
  "priority.bridge_prompt_markers": ["bridgePromptMarkers", "strings"],
  "priority.chat_triggers": ["chatTriggers", "strings"],
  "priority.hold_chat": ["holdChat", "boolean"],
  "priority.hold_chat_tools": ["holdChatTools", "boolean"],
  "priority.preempt_in_flight_chat": ["preemptInFlightChat", "boolean"],
  "priority.notify_chat": ["notifyChat", "boolean"],
  "priority.same_provider_only": ["sameProviderOnly", "boolean"],
  // OpenClaw caps operator hook budgets at this; before_tool_call fails closed on timeout.
  "priority.before_tool_call_hook_ms": ["beforeToolCallHookMs", "positive"],
  "priority.tool_hook_margin_ms": ["toolHookMarginMs", "nonNegative"],
  "priority.hook_priority": ["hookPriority", "integer"],
  "priority.bridge_wait_poll_ms": ["bridgeWaitPollMs", "positive"],
};
// Shared with the rest of the bridge: the task contract's largest timeout_minutes,
// the runner's worker heartbeat stale limit, and the task package file suffix.
export const SHARED_KEYS = {
  "timeouts.task_timeout_max_minutes": ["taskTimeoutMaxMinutes", "positive"],
  "timeouts.runner_heartbeat_stale_seconds": ["workerStaleSeconds", "positive"],
  "executors.task_file_suffix": ["taskFileSuffix", "string"],
};

const CHECKS = {
  string: (v) => typeof v === "string" && v.trim() !== "",
  strings: (v) => Array.isArray(v) && v.every((s) => typeof s === "string" && s.trim() !== ""),
  boolean: (v) => typeof v === "boolean",
  positive: (v) => typeof v === "number" && Number.isFinite(v) && v > 0,
  nonNegative: (v) => typeof v === "number" && Number.isFinite(v) && v >= 0,
  integer: (v) => Number.isInteger(v),
};

function dig(obj, dotted) {
  return dotted.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

// cfg: parsed bridge.config.json. Returns { settings, problems }; settings is
// usable only when problems is empty.
export function resolveSettings(cfg) {
  const settings = {};
  const problems = [];
  for (const [key, [name, kind]] of Object.entries({ ...PRIORITY_KEYS, ...SHARED_KEYS })) {
    const v = dig(cfg, key);
    if (v === undefined) problems.push(`${key} (missing)`);
    else if (!CHECKS[kind](v)) problems.push(`${key} (not ${kind})`);
    else settings[name] = Array.isArray(v) ? v.map((s) => s.trim()) : typeof v === "string" ? v.trim() : v;
  }
  return { settings, problems };
}

export function readBridgeConfig(file) {
  try {
    return { cfg: JSON.parse(fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "")) };
  } catch (err) {
    return { error: `cannot read ${file || "(no path)"}: ${err && err.message ? err.message : err}` };
  }
}

export function loadSettings(file) {
  const { cfg, error } = readBridgeConfig(file);
  if (error) return { settings: {}, problems: [error] };
  return resolveSettings(cfg);
}

const pluginFolder = () => path.dirname(fileURLToPath(import.meta.url));

function manifestId(dir = pluginFolder()) {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, PLUGIN_MANIFEST_FILE), "utf8").replace(/^\uFEFF/, "")).id || "";
  } catch {
    return "";
  }
}

// "~/..." the way the gateway expands its plugin load paths (its service sets USERPROFILE/HOME).
function expandHome(p) {
  const home = process.env.USERPROFILE || process.env.HOME || os.homedir();
  return path.resolve(String(p).replace(/^~(?=$|[\\/])/, home));
}

function loadPaths(gatewayConfig) {
  const paths = gatewayConfig && gatewayConfig.plugins && gatewayConfig.plugins.load && gatewayConfig.plugins.load.paths;
  return Array.isArray(paths) ? paths.filter((p) => typeof p === "string" && p.trim()) : [];
}

// The gateway imports a temporary copy of this file, so bridge.config.json is found
// next to the plugin folder named in openclaw.json plugins.load.paths (the one whose
// manifest has this plugin's id). JARVIS_BRIDGE_SETTINGS overrides; without a gateway
// config (tests, direct imports) the file's own folder is used.
function bridgeConfigPath(gatewayConfig, pluginId) {
  if (process.env.JARVIS_BRIDGE_SETTINGS) return process.env.JARVIS_BRIDGE_SETTINGS;
  for (const p of loadPaths(gatewayConfig)) {
    const dir = expandHome(p);
    if (pluginId && manifestId(dir) === pluginId) return path.join(path.dirname(dir), BRIDGE_CONFIG_FILE);
  }
  try {
    return path.join(path.dirname(pluginFolder()), BRIDGE_CONFIG_FILE);
  } catch {
    return "";
  }
}

function samePath(a, b) {
  const norm = (p) => path.resolve(p).replace(/[\\/]+$/, "");
  return process.platform === "win32" ? norm(a).toLowerCase() === norm(b).toLowerCase() : norm(a) === norm(b);
}

export function taskPathPattern(suffix) {
  const esc = suffix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`[A-Za-z]:\\\\[^\\s"'<>|*?]+?${esc}`, "g");
}

// The gateway matches this id against openclaw.plugin.json. When the file is imported
// from the gateway's temporary copy, bridge.config.json is only found at register
// time, so the copied manifest supplies the id here; register() then requires
// priority.plugin_id to equal it.
const PLUGIN_ID = loadSettings(bridgeConfigPath(undefined)).settings.pluginId || manifestId();
const STATE_KEY = Symbol.for(`${PLUGIN_ID}.state.v2`);

export function createState() {
  return {
    bridges: new Map(), // bridge run id -> { provider, since, sessionKey, jobId }
    chats: new Map(), // chat run id -> chat facts
    chatSessionKeys: new Set(), // session keys that have carried interactive chat
    bridgeJobIds: new Set(),
    heldNotified: new Set(), // chat targets told "queued" in the current bridge window
    stopped: new Map(), // chat target -> chat facts, told "stopped"
    timeoutTimer: undefined, // interval that stops bridge runs past their timeout_minutes
  };
}

function sharedState() {
  if (!globalThis[STATE_KEY]) globalThis[STATE_KEY] = createState();
  return globalThis[STATE_KEY];
}

const NOTICE_QUEUED = "A Jarvis bridge job is using the model right now. Your message is queued and I'll answer as soon as the job finishes.";
const NOTICE_STOPPED = "Paused: a Jarvis bridge job needs the model, so I stopped my reply to your last message. I'll tell you when the job is done; then send it again.";
const NOTICE_DONE = "The Jarvis bridge job is finished and the model is free again. Please resend your last message.";

// Priority state and decisions, independent of OpenClaw so it can be tested.
// deps: { now(), sleep(ms), log(level, msg), abortRun(chat), notify(chat, text),
//         readFile(path) -> text, isRunActive(bridge) -> true | false | undefined (unknown) }
// toolHoldMs: longest a chat tool call may wait (bounded by the host hook budget).
export function createBridgePriority(settings, deps, state = createState(), toolHoldMs = settings.beforeToolCallHookMs - settings.toolHookMarginMs) {
  const { bridges, chats, chatSessionKeys, bridgeJobIds, heldNotified, stopped } = state;
  const taskPathRe = taskPathPattern(settings.taskFileSuffix);

  const runKey = (ctx) => ctx.runId || ctx.sessionKey || "";
  const lower = (v) => String(v || "").toLowerCase();
  const chatTarget = (c) => (c.channel && c.chatId ? c.channel + ":" + c.chatId : "");

  function isChatTrigger(ctx) {
    return settings.chatTriggers.includes(String(ctx.trigger || ""));
  }

  function isBridgeSessionKey(key) {
    key = String(key || "");
    for (const id of bridgeJobIds) if (key.includes(":cron:" + id)) return true;
    return false;
  }

  function isBridge(event, ctx) {
    if (isChatTrigger(ctx)) return false;
    if (ctx.jobId && bridgeJobIds.has(ctx.jobId)) return true;
    if (isBridgeSessionKey(ctx.sessionKey)) return true;
    if (String(ctx.trigger || "") !== "cron") return false;
    const prompt = String((event && event.prompt) || "");
    return settings.bridgePromptMarkers.some((m) => prompt.includes(m));
  }

  // A run seen by a hook without a trigger (tool / model-call hooks).
  function knownChat(ctx) {
    const key = runKey(ctx);
    if (chats.has(key)) return chats.get(key);
    if (bridges.has(key) || isBridgeSessionKey(ctx.sessionKey)) return undefined;
    if (ctx.sessionKey && chatSessionKeys.has(ctx.sessionKey)) {
      const chat = chatFacts(ctx);
      chats.set(key, chat);
      return chat;
    }
    return undefined;
  }

  // timeout_minutes of the task package named in a bridge prompt (the last
  // package path in it), capped at the contract maximum; undefined if unreadable.
  function taskTimeoutMinutes(prompt) {
    const paths = String(prompt || "").match(taskPathRe);
    if (!paths) return undefined;
    try {
      const task = JSON.parse(String(deps.readFile(paths[paths.length - 1])).replace(/^\uFEFF/, ""));
      const v = Number(task && task.timeout_minutes);
      if (Number.isFinite(v) && v > 0) return Math.min(v, settings.taskTimeoutMaxMinutes);
    } catch {}
    return undefined;
  }

  function checkAlive(b) {
    let alive;
    try {
      alive = deps.isRunActive(b);
    } catch {
      alive = undefined;
    }
    if (alive === true) b.seenAlive = deps.now();
    return alive;
  }

  // Stops every bridge run that has run for its task's timeout_minutes (the
  // contract maximum when the task's own value is unknown) and ends its window.
  async function stopOverdue() {
    const now = deps.now();
    for (const [key, b] of [...bridges]) {
      const limitMin = b.timeoutMinutes || settings.taskTimeoutMaxMinutes;
      if (b.stopping || now - b.since < limitMin * MINUTE) continue;
      b.stopping = true;
      const stopped = await deps.abortRun({ runId: key, sessionId: b.sessionId || "", sessionKey: b.sessionKey });
      await endBridge(key, false, `timeout_minutes (${limitMin}) reached; run ${stopped ? "stopped" : "not stopped (no longer active in the gateway)"}`);
    }
  }

  // Ends bridge runs whose end event never arrived: the task's timeout_minutes
  // has passed (the run is stopped), or the run was seen active and has since
  // been gone from the gateway for longer than the heartbeat stale limit (worker dead).
  async function releaseEnded() {
    await stopOverdue();
    const now = deps.now();
    for (const [key, b] of [...bridges]) {
      if (checkAlive(b) === false && b.seenAlive && now - b.seenAlive > settings.workerStaleSeconds * 1000) {
        await endBridge(key, false, `run gone from the gateway for over ${settings.workerStaleSeconds} s; worker dead`);
      }
    }
  }

  async function blockingBridges(chatProvider) {
    await releaseEnded();
    return [...bridges.values()].filter(
      (b) => !settings.sameProviderOnly || !b.provider || !chatProvider || b.provider === chatProvider,
    );
  }

  async function stopChat(chat, why) {
    chats.delete(chat.runId || chat.sessionKey);
    const ok = await deps.abortRun(chat);
    deps.log(ok ? "info" : "warn", `${ok ? "stopped" : "could not stop"} chat run ${chat.runId || "?"} (${chat.sessionKey || "?"}): ${why}`);
    const target = chatTarget(chat);
    if (ok && settings.notifyChat && target && !stopped.has(target)) {
      stopped.set(target, chat);
      await deps.notify(chat, NOTICE_STOPPED);
    }
    return ok;
  }

  async function startBridge(ctx, prompt) {
    const key = runKey(ctx);
    const bridgeProvider = lower(ctx.modelProviderId);
    const prev = bridges.get(key);
    const b = {
      provider: bridgeProvider,
      since: prev ? prev.since : deps.now(),
      sessionKey: ctx.sessionKey || (prev && prev.sessionKey) || "",
      sessionId: ctx.sessionId || (prev && prev.sessionId) || "",
      jobId: ctx.jobId || (prev && prev.jobId) || "",
      timeoutMinutes: (prev && prev.timeoutMinutes) || taskTimeoutMinutes(prompt),
      seenAlive: prev ? prev.seenAlive : undefined,
    };
    bridges.set(key, b);
    checkAlive(b);
    if (!prev) {
      const bound = b.timeoutMinutes ? `task timeout_minutes ${b.timeoutMinutes}` : `contract max ${settings.taskTimeoutMaxMinutes} min (task timeout unknown)`;
      deps.log("info", `bridge run started: ${key} (provider ${bridgeProvider || "?"}); chat on this provider now waits until it ends (${bound})`);
    }
    if (!settings.preemptInFlightChat) return;
    for (const chat of [...chats.values()]) {
      if (settings.sameProviderOnly && chat.provider && bridgeProvider && chat.provider !== bridgeProvider) continue;
      await stopChat(chat, `bridge run ${key} started`);
    }
  }

  async function endBridge(key, success, why) {
    const b = bridges.get(key);
    if (!b) return;
    bridges.delete(key);
    const mins = ((deps.now() - b.since) / MINUTE).toFixed(1);
    deps.log(why ? "warn" : "info", `bridge run finished: ${key} after ${mins} min (success=${success !== false}${why ? "; " + why : ""}); ${bridges.size} bridge run(s) still active`);
    if (bridges.size > 0) return;
    heldNotified.clear();
    for (const [target, chat] of [...stopped]) {
      stopped.delete(target);
      if (settings.notifyChat) await deps.notify(chat, NOTICE_DONE);
    }
  }

  // Waits while a bridge run blocks this provider. Returns true if released,
  // false on `deadline` (only the host tool-hook budget sets one).
  async function waitForBridges(chatProvider, deadline = Infinity) {
    while ((await blockingBridges(chatProvider)).length > 0) {
      if (deps.now() >= deadline) return false;
      await deps.sleep(Math.min(settings.bridgeWaitPollMs, Math.max(1, deadline - deps.now())));
    }
    return true;
  }

  async function holdChatTurn(ctx) {
    if (!settings.holdChat) return;
    const chatProvider = lower(ctx.modelProviderId);
    if ((await blockingBridges(chatProvider)).length === 0) return;
    const key = runKey(ctx);
    const started = deps.now();
    deps.log("info", `holding chat turn ${key} (${ctx.sessionKey || "?"}) until the bridge run finishes`);
    const chat = chatFacts(ctx);
    const target = chatTarget(chat);
    if (settings.notifyChat && target && !heldNotified.has(target) && !stopped.has(target)) {
      heldNotified.add(target);
      await deps.notify(chat, NOTICE_QUEUED);
    }
    await waitForBridges(chatProvider);
    deps.log("info", `released chat turn ${key} after ${((deps.now() - started) / 1000).toFixed(0)} s`);
  }

  function chatFacts(ctx) {
    const sessionKey = String(ctx.sessionKey || "");
    let channel = String(ctx.messageProvider || ctx.channel || "");
    let chatId = String(ctx.chatId || "");
    // Direct-chat session keys look like agent:<agent>:<channel>:direct:<peer>.
    const m = /:([a-z0-9_-]+):direct:([^:]+)$/i.exec(sessionKey);
    if (m) {
      if (!channel) channel = m[1];
      if (!chatId) chatId = m[2];
    }
    if (!chatId) chatId = String(ctx.channelId || "");
    return {
      runId: ctx.runId || "",
      sessionId: ctx.sessionId || "",
      sessionKey,
      agentId: ctx.agentId || "",
      provider: lower(ctx.modelProviderId),
      channel,
      chatId,
      accountId: String(ctx.accountId || ""),
    };
  }

  return {
    state,
    bridges,
    chats,
    bridgeJobIds,
    isBridge,
    stopOverdue,

    async beforeModelResolve(event, ctx) {
      if (isBridge(event, ctx)) {
        await startBridge(ctx, event && event.prompt);
        return;
      }
      if (!isChatTrigger(ctx)) return;
      if (ctx.sessionKey) chatSessionKeys.add(ctx.sessionKey);
      await holdChatTurn(ctx);
      const key = runKey(ctx);
      if (key) chats.set(key, chatFacts(ctx));
    },

    // Fire-and-forget in OpenClaw: records the provider actually used and stops
    // a chat model call that starts while a bridge run holds that provider.
    async modelCallStarted(event, ctx) {
      const key = runKey(ctx);
      const provider = lower(ctx.modelProviderId || (event && event.provider));
      const bridge = bridges.get(key);
      if (bridge) {
        if (provider) bridge.provider = provider;
        if (ctx.sessionId) bridge.sessionId = ctx.sessionId;
        checkAlive(bridge);
        return;
      }
      if (isBridgeSessionKey(ctx.sessionKey)) {
        await startBridge({ ...ctx, modelProviderId: provider });
        return;
      }
      const chat = knownChat(ctx);
      if (!chat) return;
      if (provider) chat.provider = provider;
      if (ctx.sessionId) chat.sessionId = ctx.sessionId;
      if (!settings.preemptInFlightChat || (await blockingBridges(chat.provider)).length === 0) return;
      await stopChat(chat, "chat model call started during a bridge run");
    },

    // Awaited by OpenClaw before every tool call: pauses a chat run between model calls.
    async beforeToolCall(_event, ctx) {
      const chat = knownChat(ctx);
      if (!chat) return undefined;
      if (ctx.sessionId) chat.sessionId = ctx.sessionId;
      if (!settings.holdChatTools || (await blockingBridges(chat.provider)).length === 0) return undefined;
      const started = deps.now();
      deps.log("info", `pausing chat run ${chat.runId || "?"} (${chat.sessionKey || "?"}) at a tool call until the bridge run finishes`);
      if (toolHoldMs > 0 && (await waitForBridges(chat.provider, started + toolHoldMs))) {
        deps.log("info", `resumed chat run ${chat.runId || "?"} after ${((deps.now() - started) / 1000).toFixed(0)} s`);
        return undefined;
      }
      if (!settings.preemptInFlightChat) {
        deps.log("warn", `chat run ${chat.runId || "?"} paused ${(toolHoldMs / 1000).toFixed(0)} s; letting it continue alongside the bridge run`);
        return undefined;
      }
      await stopChat(chat, "tool pause reached its limit while the bridge run is still active");
      return { block: true, blockReason: "Stopped so a Jarvis bridge job can use the model." };
    },

    async agentEnd(event, ctx) {
      const key = runKey(ctx);
      chats.delete(key);
      await endBridge(key, event && event.success);
    },

    async cronFinished(job) {
      if (!job || !bridgeJobIds.has(job.id)) return;
      for (const [key, b] of [...bridges]) {
        if (b.jobId === job.id || b.sessionKey.includes(":cron:" + job.id)) await endBridge(key, job.state && job.state.lastRunStatus === "ok");
      }
    },

    setBridgeJobs(jobs, names) {
      const wanted = new Set(names);
      for (const job of jobs || []) {
        if (!job || typeof job.id !== "string") continue;
        if (wanted.has(job.name)) bridgeJobIds.add(job.id);
        else bridgeJobIds.delete(job.id);
      }
    },
  };
}

// The CLI (`gateway call sessions.abort`) is not authorized from inside the
// gateway service, so runs are aborted in-process. OpenClaw keeps active runs in
// a globalThis registry, so any copy of the SDK module sees the gateway's runs.
function findOpenClawRoot(start) {
  let dir = start ? path.dirname(start) : "";
  for (let i = 0; dir && i < 8; i++) {
    try {
      const pkg = JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8"));
      if (pkg && pkg.name === "openclaw") return dir;
    } catch {}
    const up = path.dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return "";
}

let agentHarness;
async function loadAgentHarness() {
  if (agentHarness) return agentHarness;
  const errors = [];
  try {
    agentHarness = await import("openclaw/plugin-sdk/agent-harness");
    return agentHarness;
  } catch (err) {
    errors.push(String(err));
  }
  const root = findOpenClawRoot(process.argv[1] || "");
  if (root) {
    try {
      agentHarness = await import(pathToFileURL(path.join(root, "dist", "plugin-sdk", "agent-harness.js")).href);
      return agentHarness;
    } catch (err) {
      errors.push(String(err));
    }
  }
  throw new Error("OpenClaw agent-harness SDK not found: " + errors.join("; "));
}

// The operator's before_tool_call budget for this plugin in openclaw.json; `fallback` when unset.
function hookBudgetMs(cfg, pluginId, hookName, fallback) {
  const hooks = cfg && cfg.plugins && cfg.plugins.entries && cfg.plugins.entries[pluginId] && cfg.plugins.entries[pluginId].hooks;
  const per = hooks && hooks.timeouts && hooks.timeouts[hookName];
  if (typeof per === "number" && per > 0) return per;
  if (hooks && typeof hooks.timeoutMs === "number" && hooks.timeoutMs > 0) return hooks.timeoutMs;
  return fallback;
}

// Bridge automations whose own run limit would stop a task before the contract
// maximum (OpenClaw applies its default limit when timeoutSeconds is unset; 0 = none).
export function shortJobCaps(jobs, names, maxMinutes) {
  const wanted = new Set(names);
  return (jobs || [])
    .filter((j) => j && wanted.has(j.name) && j.payload)
    .filter((j) => {
      const t = j.payload.timeoutSeconds;
      return typeof t !== "number" || (t > 0 && t * 1000 < maxMinutes * MINUTE);
    })
    .map((j) => `${j.name} (${j.id}) timeoutSeconds=${j.payload.timeoutSeconds === undefined ? "unset" : j.payload.timeoutSeconds}`);
}

export function describeSettings(s) {
  return Object.entries({ ...PRIORITY_KEYS, ...SHARED_KEYS })
    .map(([key, [name]]) => `${key}=${JSON.stringify(s[name])}`)
    .join(", ");
}

export default {
  id: PLUGIN_ID,
  name: "Jarvis Bridge priority",
  description: "Bridge task runs take priority over interactive chat on the shared local model.",
  register(api) {
    const configFile = bridgeConfigPath(api.config, PLUGIN_ID);
    const { settings, problems } = loadSettings(configFile);
    const shared = sharedState();
    if (shared.timeoutTimer) clearInterval(shared.timeoutTimer);
    shared.timeoutTimer = undefined;
    const log = (level, msg) => {
      const fn = api.logger && (api.logger[level] || api.logger.warn || api.logger.info);
      if (fn) fn.call(api.logger, `[${PLUGIN_ID}] ${msg}`);
    };
    if (problems.length === 0 && settings.pluginId !== PLUGIN_ID) {
      problems.push(`priority.plugin_id is "${settings.pluginId}" but this plugin loaded as "${PLUGIN_ID}" (reload needs a gateway restart)`);
    }
    const manifest = manifestId();
    if (problems.length === 0 && manifest && manifest !== settings.pluginId) {
      problems.push(`priority.plugin_id "${settings.pluginId}" does not match ${PLUGIN_MANIFEST_FILE} id "${manifest}"`);
    }
    if (problems.length > 0) {
      log("error", `v${VERSION} NOT ACTIVE (no hooks registered; chat is not held for bridge runs): ${configFile}: ${problems.join("; ")}`);
      return;
    }
    const ignored = api.pluginConfig && typeof api.pluginConfig === "object" ? Object.keys(api.pluginConfig) : [];
    if (ignored.length > 0) {
      log("warn", `openclaw.json plugins.entries.${PLUGIN_ID}.config (${ignored.join(", ")}) is ignored; these settings live in ${configFile} section "priority"`);
    }
    const gatewayPaths = loadPaths(api.config);
    if (gatewayPaths.length > 0 && !gatewayPaths.some((p) => samePath(expandHome(p), expandHome(settings.pluginDir)))) {
      log("warn", `priority.plugin_dir ${settings.pluginDir} is not in openclaw.json plugins.load.paths (${gatewayPaths.join(", ")}); keep them equal`);
    }
    const currentConfig = () => {
      try {
        const c = api.runtime && api.runtime.config && typeof api.runtime.config.current === "function" ? api.runtime.config.current() : undefined;
        return c || api.config || {};
      } catch {
        return api.config || {};
      }
    };
    const toolBudget = hookBudgetMs(api.config, PLUGIN_ID, "before_tool_call", settings.beforeToolCallHookMs);
    const toolHoldMs = Math.max(0, Math.min(settings.beforeToolCallHookMs, toolBudget) - settings.toolHookMarginMs);
    const priority = createBridgePriority(
      settings,
      {
        now: () => Date.now(),
        sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
        log,
        readFile: (file) => fs.readFileSync(file, "utf8"),
        isRunActive(bridge) {
          if (!bridge.sessionKey) return undefined;
          if (!agentHarness) {
            loadAgentHarness().catch(() => {});
            return undefined;
          }
          if (typeof agentHarness.resolveActiveEmbeddedRunSessionId !== "function") return undefined;
          return Boolean(agentHarness.resolveActiveEmbeddedRunSessionId(bridge.sessionKey));
        },
        async abortRun(chat) {
          try {
            const sdk = await loadAgentHarness();
            let sessionId = chat.sessionId;
            if (!sessionId && chat.sessionKey && typeof sdk.resolveActiveEmbeddedRunSessionId === "function") {
              sessionId = sdk.resolveActiveEmbeddedRunSessionId(chat.sessionKey) || "";
            }
            if (!sessionId) {
              log("warn", `no session id for chat run ${chat.runId || chat.sessionKey}; cannot stop it`);
              return false;
            }
            const abort = sdk.abortAgentHarnessRun || sdk.abortEmbeddedAgentRun;
            if (typeof abort !== "function") throw new Error("OpenClaw SDK has no run abort function");
            return abort(sessionId) === true;
          } catch (err) {
            log("warn", `run abort failed: ${err}`);
            return false;
          }
        },
        async notify(chat, text) {
          try {
            const outbound = api.runtime && api.runtime.channel && api.runtime.channel.outbound;
            const adapter = outbound && chat.channel ? await outbound.loadAdapter(chat.channel) : undefined;
            if (!adapter || typeof adapter.sendText !== "function") {
              log("info", `no outbound delivery for channel ${chat.channel || "?"}; skipped chat notice`);
              return;
            }
            await adapter.sendText({ cfg: currentConfig(), to: chat.chatId, text, accountId: chat.accountId || undefined, silent: true });
            log("info", `chat notice sent to ${chat.channel}:${chat.chatId}`);
          } catch (err) {
            log("warn", `chat notice to ${chat.channel}:${chat.chatId} failed: ${err}`);
          }
        },
      },
      shared,
      toolHoldMs,
    );
    shared.timeoutTimer = setInterval(() => {
      priority.stopOverdue().catch((err) => log("warn", `timeout_minutes check failed: ${err}`));
    }, settings.bridgeWaitPollMs);
    if (typeof shared.timeoutTimer.unref === "function") shared.timeoutTimer.unref();
    const warnShortCaps = (jobs) => {
      const short = shortJobCaps(jobs, settings.bridgeJobNames, settings.taskTimeoutMaxMinutes);
      if (short.length > 0) {
        log("warn", `bridge automation run limit is below the ${settings.taskTimeoutMaxMinutes}-min contract maximum, so longer tasks are cut short: ${short.join(", ")}; set timeoutSeconds to ${(settings.taskTimeoutMaxMinutes * MINUTE) / 1000}`);
      }
    };

    const refreshJobs = async (getCron) => {
      try {
        const cron = typeof getCron === "function" ? getCron() : undefined;
        if (!cron || typeof cron.list !== "function") return;
        const jobs = await cron.list({ includeDisabled: true });
        priority.setBridgeJobs(jobs, settings.bridgeJobNames);
        warnShortCaps(jobs);
        log("info", `bridge automations: ${[...priority.bridgeJobIds].join(", ") || "none found (prompt markers still apply)"}`);
      } catch (err) {
        log("warn", `could not list automations: ${err}`);
      }
    };

    api.on("before_model_resolve", (event, ctx) => priority.beforeModelResolve(event, ctx || {}), { priority: settings.hookPriority });
    api.on("model_call_started", (event, ctx) => priority.modelCallStarted(event || {}, ctx || {}));
    api.on("before_tool_call", (event, ctx) => priority.beforeToolCall(event || {}, ctx || {}), {
      priority: settings.hookPriority,
      timeoutMs: settings.beforeToolCallHookMs,
    });
    api.on("agent_end", (event, ctx) => priority.agentEnd(event, ctx || {}));
    api.on("cron_reconciled", (_event, ctx) => refreshJobs(ctx && ctx.getCron));
    api.on("cron_changed", (event) => {
      const job = event && event.job;
      if (!job || typeof job.id !== "string") return;
      if (event.reason === "removed") priority.bridgeJobIds.delete(job.id);
      else priority.setBridgeJobs([job], settings.bridgeJobNames);
      if (event.reason === "finished") return priority.cronFinished(job);
      warnShortCaps([job]);
    });
    log(
      "info",
      `loaded v${VERSION} (hold chat=${settings.holdChat} for the bridge task's lifetime, pause chat tools=${settings.holdChatTools} up to ${(toolHoldMs / 1000).toFixed(0)} s, ` +
        `release on end / run stopped at its task's timeout_minutes (max ${settings.taskTimeoutMaxMinutes}, checked every ${settings.bridgeWaitPollMs} ms) / run gone ${settings.workerStaleSeconds} s, ` +
        `preempt=${settings.preemptInFlightChat}, notify=${settings.notifyChat}, bridge jobs=${settings.bridgeJobNames.join(", ")}, ` +
        `tracked: ${priority.bridges.size} bridge / ${priority.chats.size} chat run(s))`,
    );
    log("info", `settings from ${configFile}: ${describeSettings(settings)}`);
  },
};
