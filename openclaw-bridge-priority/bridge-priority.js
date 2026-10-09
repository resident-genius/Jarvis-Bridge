// Jarvis Bridge priority -- OpenClaw plugin (Node standard library only).
//
// Bridge task runs (the "jarvis-bridge-inbox-watcher" automation) and {{OPERATOR_NAME}}'s
// interactive chat share one local model. The model server answers one request
// at a time, and OpenClaw queues cron runs as "background" work, so a chat that
// keeps talking can starve a bridge run until it times out. This plugin flips
// that order:
//   * bridge run starts   -> every chat run using the same model provider is
//                            stopped (in-process run abort) and the chat is told why;
//   * while it is active  -> new chat turns wait in before_model_resolve, a chat
//                            run that reaches a tool call waits in before_tool_call,
//                            and a chat model call that still starts is stopped;
//   * bridge run ends     -> held chat turns and tool calls continue on their own.
// State lives on globalThis so a plugin hot reload does not forget in-flight runs.
// On Windows the gateway restarts in-process and Node never re-imports a file it
// already loaded: after editing this file, restart the OpenClawGateway service
// (or reboot), or rename the file and update package.json "openclaw.extensions".
// Documentation: BRIDGE-PRIORITY.md next to this folder.

import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const PLUGIN_ID = "jarvis-bridge-priority";
const VERSION = "2.0.0";
const MINUTE = 60 * 1000;
const STATE_KEY = Symbol.for("jarvis-bridge-priority.state.v2");
// OpenClaw caps operator hook budgets at 600000 ms; before_tool_call fails closed on timeout.
const TOOL_HOOK_BUDGET_MS = 600000;
const TOOL_HOOK_MARGIN_MS = 20000;

const DEFAULTS = {
  bridgeJobNames: ["jarvis-bridge-inbox-watcher"],
  bridgePromptMarkers: [".jarvis-task.json"],
  chatTriggers: ["user", "manual"],
  holdChat: true,
  holdChatTools: true,
  preemptInFlightChat: true,
  notifyChat: true,
  sameProviderOnly: true,
  maxChatHoldMinutes: 35,
  maxToolHoldMinutes: 9,
  maxBridgeHoldMinutes: 35,
};

export function resolveSettings(raw) {
  const cfg = raw && typeof raw === "object" ? raw : {};
  const list = (v, d) =>
    Array.isArray(v) ? v.filter((s) => typeof s === "string" && s.trim()).map((s) => s.trim()) : d;
  const bool = (v, d) => (typeof v === "boolean" ? v : d);
  const minutes = (v, d) => (typeof v === "number" && Number.isFinite(v) && v >= 1 ? v : d);
  return {
    bridgeJobNames: list(cfg.bridgeJobNames, DEFAULTS.bridgeJobNames),
    bridgePromptMarkers: list(cfg.bridgePromptMarkers, DEFAULTS.bridgePromptMarkers),
    chatTriggers: list(cfg.chatTriggers, DEFAULTS.chatTriggers),
    holdChat: bool(cfg.holdChat, DEFAULTS.holdChat),
    holdChatTools: bool(cfg.holdChatTools, DEFAULTS.holdChatTools),
    preemptInFlightChat: bool(cfg.preemptInFlightChat, DEFAULTS.preemptInFlightChat),
    notifyChat: bool(cfg.notifyChat, DEFAULTS.notifyChat),
    sameProviderOnly: bool(cfg.sameProviderOnly, DEFAULTS.sameProviderOnly),
    maxChatHoldMinutes: minutes(cfg.maxChatHoldMinutes, DEFAULTS.maxChatHoldMinutes),
    maxToolHoldMinutes: minutes(cfg.maxToolHoldMinutes, DEFAULTS.maxToolHoldMinutes),
    maxBridgeHoldMinutes: minutes(cfg.maxBridgeHoldMinutes, DEFAULTS.maxBridgeHoldMinutes),
  };
}

export function createState() {
  return {
    bridges: new Map(), // bridge run id -> { provider, since, sessionKey, jobId }
    chats: new Map(), // chat run id -> chat facts
    chatSessionKeys: new Set(), // session keys that have carried interactive chat
    bridgeJobIds: new Set(),
    heldNotified: new Set(), // chat targets told "queued" in the current bridge window
    stopped: new Map(), // chat target -> chat facts, told "stopped"
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
// deps: { now(), sleep(ms), log(level, msg), abortRun(chat), notify(chat, text) }
// toolHoldMs: longest a chat tool call may wait (bounded by the host hook budget).
export function createBridgePriority(settings, deps, state = createState(), toolHoldMs = settings.maxToolHoldMinutes * MINUTE) {
  const { bridges, chats, chatSessionKeys, bridgeJobIds, heldNotified, stopped } = state;

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

  function pruneExpired() {
    const limit = settings.maxBridgeHoldMinutes * MINUTE;
    for (const [key, b] of bridges) {
      if (deps.now() - b.since > limit) {
        bridges.delete(key);
        deps.log("warn", `bridge run ${key} passed ${settings.maxBridgeHoldMinutes} min without ending; no longer blocking chat`);
      }
    }
    if (bridges.size === 0) heldNotified.clear();
  }

  function blockingBridges(chatProvider) {
    pruneExpired();
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

  async function startBridge(ctx) {
    const key = runKey(ctx);
    const bridgeProvider = lower(ctx.modelProviderId);
    const known = bridges.has(key);
    bridges.set(key, { provider: bridgeProvider, since: known ? bridges.get(key).since : deps.now(), sessionKey: ctx.sessionKey || "", jobId: ctx.jobId || "" });
    if (!known) deps.log("info", `bridge run started: ${key} (provider ${bridgeProvider || "?"}); chat on this provider now waits`);
    if (!settings.preemptInFlightChat) return;
    for (const chat of [...chats.values()]) {
      if (settings.sameProviderOnly && chat.provider && bridgeProvider && chat.provider !== bridgeProvider) continue;
      await stopChat(chat, `bridge run ${key} started`);
    }
  }

  async function endBridge(key, success) {
    const b = bridges.get(key);
    if (!b) return;
    bridges.delete(key);
    const mins = ((deps.now() - b.since) / MINUTE).toFixed(1);
    deps.log("info", `bridge run finished: ${key} after ${mins} min (success=${success !== false}); ${bridges.size} bridge run(s) still active`);
    if (bridges.size > 0) return;
    heldNotified.clear();
    for (const [target, chat] of [...stopped]) {
      stopped.delete(target);
      if (settings.notifyChat) await deps.notify(chat, NOTICE_DONE);
    }
  }

  // Waits while a bridge run blocks this provider. Returns true if released, false on deadline.
  async function waitForBridges(chatProvider, deadline) {
    while (blockingBridges(chatProvider).length > 0) {
      if (deps.now() >= deadline) return false;
      await deps.sleep(Math.min(2000, Math.max(1, deadline - deps.now())));
    }
    return true;
  }

  async function holdChatTurn(ctx) {
    if (!settings.holdChat) return;
    const chatProvider = lower(ctx.modelProviderId);
    if (blockingBridges(chatProvider).length === 0) return;
    const key = runKey(ctx);
    const started = deps.now();
    deps.log("info", `holding chat turn ${key} (${ctx.sessionKey || "?"}) until the bridge run finishes`);
    const chat = chatFacts(ctx);
    const target = chatTarget(chat);
    if (settings.notifyChat && target && !heldNotified.has(target) && !stopped.has(target)) {
      heldNotified.add(target);
      await deps.notify(chat, NOTICE_QUEUED);
    }
    if (!(await waitForBridges(chatProvider, started + settings.maxChatHoldMinutes * MINUTE))) {
      deps.log("warn", `chat turn ${key} waited ${settings.maxChatHoldMinutes} min; letting it run alongside the bridge run`);
      return;
    }
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

    async beforeModelResolve(event, ctx) {
      if (isBridge(event, ctx)) {
        await startBridge(ctx);
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
      if (!settings.preemptInFlightChat || blockingBridges(chat.provider).length === 0) return;
      await stopChat(chat, "chat model call started during a bridge run");
    },

    // Awaited by OpenClaw before every tool call: pauses a chat run between model calls.
    async beforeToolCall(_event, ctx) {
      const chat = knownChat(ctx);
      if (!chat) return undefined;
      if (ctx.sessionId) chat.sessionId = ctx.sessionId;
      if (!settings.holdChatTools || blockingBridges(chat.provider).length === 0) return undefined;
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

function hookBudgetMs(cfg, hookName) {
  const hooks = cfg && cfg.plugins && cfg.plugins.entries && cfg.plugins.entries[PLUGIN_ID] && cfg.plugins.entries[PLUGIN_ID].hooks;
  const per = hooks && hooks.timeouts && hooks.timeouts[hookName];
  if (typeof per === "number" && per > 0) return per;
  if (hooks && typeof hooks.timeoutMs === "number" && hooks.timeoutMs > 0) return hooks.timeoutMs;
  return TOOL_HOOK_BUDGET_MS;
}

export default {
  id: PLUGIN_ID,
  name: "Jarvis Bridge priority",
  description: "Bridge task runs take priority over interactive chat on the shared local model.",
  register(api) {
    const settings = resolveSettings(api.pluginConfig);
    const log = (level, msg) => {
      const fn = api.logger && (api.logger[level] || api.logger.info);
      if (fn) fn.call(api.logger, `[${PLUGIN_ID}] ${msg}`);
    };
    const currentConfig = () => {
      try {
        const c = api.runtime && api.runtime.config && typeof api.runtime.config.current === "function" ? api.runtime.config.current() : undefined;
        return c || api.config || {};
      } catch {
        return api.config || {};
      }
    };
    const toolBudget = hookBudgetMs(api.config, "before_tool_call");
    const toolHoldMs = Math.max(0, Math.min(settings.maxToolHoldMinutes * MINUTE, toolBudget - TOOL_HOOK_MARGIN_MS));
    const priority = createBridgePriority(
      settings,
      {
        now: () => Date.now(),
        sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
        log,
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
      sharedState(),
      toolHoldMs,
    );

    const refreshJobs = async (getCron) => {
      try {
        const cron = typeof getCron === "function" ? getCron() : undefined;
        if (!cron || typeof cron.list !== "function") return;
        priority.setBridgeJobs(await cron.list({ includeDisabled: true }), settings.bridgeJobNames);
        log("info", `bridge automations: ${[...priority.bridgeJobIds].join(", ") || "none found (prompt markers still apply)"}`);
      } catch (err) {
        log("warn", `could not list automations: ${err}`);
      }
    };

    api.on("before_model_resolve", (event, ctx) => priority.beforeModelResolve(event, ctx || {}), { priority: 100 });
    api.on("model_call_started", (event, ctx) => priority.modelCallStarted(event || {}, ctx || {}));
    api.on("before_tool_call", (event, ctx) => priority.beforeToolCall(event || {}, ctx || {}), { priority: 100, timeoutMs: TOOL_HOOK_BUDGET_MS });
    api.on("agent_end", (event, ctx) => priority.agentEnd(event, ctx || {}));
    api.on("cron_reconciled", (_event, ctx) => refreshJobs(ctx && ctx.getCron));
    api.on("cron_changed", (event) => {
      const job = event && event.job;
      if (!job || typeof job.id !== "string") return;
      if (event.reason === "removed") priority.bridgeJobIds.delete(job.id);
      else priority.setBridgeJobs([job], settings.bridgeJobNames);
      if (event.reason === "finished") return priority.cronFinished(job);
    });
    log(
      "info",
      `loaded v${VERSION} (hold chat=${settings.holdChat}, pause chat tools=${settings.holdChatTools} up to ${(toolHoldMs / 1000).toFixed(0)} s, ` +
        `preempt=${settings.preemptInFlightChat}, notify=${settings.notifyChat}, bridge jobs=${settings.bridgeJobNames.join(", ")}, ` +
        `tracked: ${priority.bridges.size} bridge / ${priority.chats.size} chat run(s))`,
    );
  },
};
