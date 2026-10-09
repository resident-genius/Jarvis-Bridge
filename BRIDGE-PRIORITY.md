# Bridge jobs go before chat

Jarvis bridge tasks take priority over interactive chat with Neo on the shared
local Ollama provider (Arc Pro B60 on 11434, reached through the guard on
11436). This is done by one small OpenClaw plugin,
`jarvis-bridge-priority` (version 2.0.0), that runs inside the gateway.

## Why bridge runs stalled

- Neo's bridge runs (the `jarvis-bridge-inbox-watcher` automation) use
  `ollama/qwen3.8:27b`. {{OPERATOR_NAME}}'s Telegram DM is pinned to Glimmer:
  `channels.modelByChannel.telegram.{{TELEGRAM_CHAT_ID}}` =
  `ollama/muse-glimmer:30b-q4_K_M-avatar-vision-tools` (vision + tools). It
  is not `llama-npu`. Both models are on the `ollama` provider, so they
  compete for the same Ollama on the B60. (Updated 2026-10-07 by
  the openclaw-harden task. The daily guard
  `workspace\tools\model-wiring-check.ps1` alerts {{OPERATOR_NAME}} if this pin drifts.)
- Ollama answers one request at a time. With a 131k context and
  `keep_alive: 0`, one model call takes several minutes on this machine.
- One chat message can be a long run with many model calls (for example a
  browser task). OpenClaw gives automation runs no priority over it.
- 2026-09-29 05:17: the watcher run waited behind chat and hit its 30-minute
  limit ("job execution timed out, last phase: model-call-started").
- 2026-09-29 06:27: the same thing happened again even though plugin v1.0 was
  installed. v1.0 only gated a chat run once, when it started, so a long chat
  run kept calling the model; a plugin hot reload at 06:13 had also wiped its
  list of running chats, and its "stop the chat" call was refused by the
  gateway (`unauthorized`). The fixed v1.1 code never loaded (see "Restarting"
  below).

## What was changed

| What | Where |
|---|---|
| The plugin (Node standard library only, no packages) | `jarvis-bridge\openclaw-bridge-priority\bridge-priority.js` (+ `openclaw.plugin.json`, `package.json`) |
| Plugin tests | `jarvis-bridge\openclaw-bridge-priority\bridge-priority.test.mjs` |
| OpenClaw config: plugin load path and plugin settings | `%USERPROFILE%\.openclaw\openclaw.json`, keys `plugins.load.paths` and `plugins.entries.jarvis-bridge-priority` |
| The config patch that was applied | `jarvis-bridge\openclaw-bridge-priority\openclaw.patch.json5` |
| Live test script | `jarvis-bridge\smoke\priority_live_test.ps1` |

The plugin file used to be `index.js`; it was renamed to `bridge-priority.js`
so the running gateway would load the new code (see "Restarting").

Nothing else was changed: the bridge (`bridge_core.py`, `bridge_gui.py`,
`Invoke-JarvisTask.ps1`), the email contract, the Gmail/DKIM/allowlist checks,
the watcher automation and its trigger script, the Telegram settings, and the
Ollama settings are as they were.

## How it works

1. **A bridge run starts.** A run counts as a bridge run when it comes from
   the `jarvis-bridge-inbox-watcher` automation, or when it is an automation
   run whose prompt names a `.jarvis-task.json` package. The plugin marks the
   bridge as active for that model provider (`ollama`).
2. **Chat already using the model is stopped.** Every chat run on the same
   provider is stopped at once (in-process, the same way `/stop` does), so
   Ollama is free for the bridge. {{OPERATOR_NAME}} gets a short Telegram note: his reply
   was paused for a bridge job, and he should resend it when told the job is
   done.
3. **While the bridge run is active:**
   - a new chat message waits before it asks the model; {{OPERATOR_NAME}} gets one note
     that it is queued, and it runs by itself when the bridge run ends;
   - a chat run that reaches a tool call waits there (a real pause, nothing is
     lost) and carries on when the bridge run ends;
   - a chat model call that still starts on the same provider is stopped.
4. **The bridge run ends** (finished, failed or timed out). Waiting chat
   continues. If a reply was stopped, {{OPERATOR_NAME}} gets a note that the model is
   free again.

Chat on a different provider (for example the NPU `llama-npu` model) is never
held or stopped, because it does not compete for Qwen. The plugin keeps its
list of running bridge and chat runs in process memory that survives plugin
reloads.

### Safety limits

- A queued chat message never waits longer than `maxChatHoldMinutes` (34).
  After that it runs anyway.
- A chat tool call waits at most `maxToolHoldMinutes` (9). OpenClaw lets a
  plugin hold a tool call for at most 10 minutes, and a hold that runs out
  would fail the tool call, so the plugin stops that chat run cleanly instead
  (and tells {{OPERATOR_NAME}}) if the bridge run is still going.
- A bridge run that never reports its end stops blocking chat after
  `maxBridgeHoldMinutes` (35). The watcher's own limit is 30 minutes.
- If the plugin fails to load, OpenClaw behaves exactly as before.

## Settings (change them here)

In `%USERPROFILE%\.openclaw\openclaw.json`, under
`plugins.entries.jarvis-bridge-priority`:

| Setting | Current value | Meaning |
|---|---|---|
| `hooks.allowConversationAccess` | `true` | Required so the plugin can see chat and automation runs. |
| `hooks.timeouts.before_tool_call` | `600000` | How long (ms) OpenClaw lets the plugin hold one chat tool call. 600000 is OpenClaw's maximum. |
| `config.holdChat` | `true` | New chat messages wait while a bridge run is active. |
| `config.holdChatTools` | not set (default `true`) | A running chat pauses at its next tool call while a bridge run is active. |
| `config.preemptInFlightChat` | `true` | Chat using the model is stopped when a bridge run starts or when a chat model call starts during one. Set to `false` to only queue and pause. |
| `config.notifyChat` | `true` | Send {{OPERATOR_NAME}} the short "queued", "paused" and "done" notes. |
| `config.sameProviderOnly` | `true` | Only hold or stop chat that uses the same model provider as the bridge run. |
| `config.maxChatHoldMinutes` | `34` | Longest a queued chat message waits. |
| `config.maxToolHoldMinutes` | not set (default `9`) | Longest a chat tool call waits. |
| `config.maxBridgeHoldMinutes` | `35` | Longest one bridge run can block chat. |
| `config.bridgeJobNames` | `["jarvis-bridge-inbox-watcher"]` | Automation names whose runs count as bridge runs. |
| `config.bridgePromptMarkers` | `[".jarvis-task.json"]` | Automation runs whose prompt contains this text count as bridge runs. |
| `config.chatTriggers` | `["user", "manual"]` | Which runs count as interactive chat. |

`plugins.load.paths` is `["~/jarvis-bridge/openclaw-bridge-priority/"]` (the
trailing `/` was added only to make the gateway restart and load v2.0).

- **Turn it off:** set `plugins.entries.jarvis-bridge-priority.enabled` to
  `false`.
- **Remove it:** delete that entry and the path in `plugins.load.paths`, or
  copy back a backup of `openclaw.json` saved before the plugin was installed.

## Restarting (important when editing the plugin)

On this Windows machine the gateway restarts *inside the same process*
("restart mode: in-process restart" in the log), and Node never re-reads a
JavaScript file it has already loaded. So:

- **Settings changes** apply by themselves (the gateway reloads the config).
- **Code changes** to `bridge-priority.js` only take effect after the
  `OpenClawGateway` Windows service is restarted (needs an administrator) or
  the PC is rebooted. Without admin rights: rename the file, update
  `"openclaw.extensions"` in `package.json`, and change `plugins.load.paths`
  (any edit), which makes the gateway restart and load the renamed file.
- Check the gateway log for the version line, for example
  `[jarvis-bridge-priority] loaded v2.0.0 (...)`.

## Checking it

- Gateway log: `%TEMP%\openclaw\openclaw-<date>.log`. Plugin lines start with
  `[jarvis-bridge-priority]`, for example `bridge run started`,
  `stopped chat run`, `holding chat turn`, `pausing chat run ... at a tool
  call`, `bridge run finished`.
- Plugin tests: `node --test openclaw-bridge-priority\bridge-priority.test.mjs`
  (use the gateway's Node, `C:\Program Files\nodejs\node.exe`).
- Live test: `.\smoke\priority_live_test.ps1 -SimulateChat`. This runs two
  throwaway chat sessions on Qwen, drops a small test task into the OpenClaw
  inbox, waits for its result file, and saves the plugin's log lines to
  `smoke\results\priority-<task-id>.plugin-log.txt`.

## Limits

- **Stopping, not pausing, for a reply that is mid-answer.** Ollama cannot
  pause a model call halfway, so a chat reply that is using the model when a
  bridge job arrives is stopped; {{OPERATOR_NAME}} resends that one message (the plugin
  tells him when). A chat that is between steps is paused and loses nothing.
  Messages sent while the bridge runs are queued and answered automatically.
- **The bridge itself is still slow.** Priority removes the wait for chat.
  Each Qwen call still reloads the 20 GB model, because `keep_alive` is `0` in
  both the OpenClaw provider settings and the Ollama service, so a small
  bridge task still takes roughly 10 to 15 minutes. Changing that is outside
  this change.
