# Where Neo's bridge work goes

Every bridge task that goes to Neo (the `openclaw` executor) now gets its own
folder inside Neo's normal OpenClaw workspace. Neo's working files, proof
screenshots and attachments go there, never to the Desktop.

## The folder

| | Path |
|---|---|
| Neo's OpenClaw workspace (checked on disk) | `%USERPROFILE%\.openclaw\workspace` |
| All bridge task folders | `%USERPROFILE%\.openclaw\workspace\jarvis-bridge-tasks` |
| One task's folder | `%USERPROFILE%\.openclaw\workspace\jarvis-bridge-tasks\<task-id>` |

`%USERPROFILE%` is the Windows user's profile folder, so the same path works
for any user.
Paste it into the Explorer address bar; the `.openclaw` folder is hidden by
default, but typing the path opens it.

How the workspace was found: OpenClaw's own config
(`%USERPROFILE%\.openclaw\openclaw.json`, key `agents.defaults.workspace`) sets
it to `~/.openclaw/workspace`, and that folder exists and holds Neo's
`AGENTS.md`, `MEMORY.md`, `skills\openclaw-bridge\SKILL.md` and so on.

The `jarvis-bridge-tasks` folder is created the first time a task arrives.

## Why the smoke test landed on the Desktop

The bridge used to pass Jarvis's `workspace` value to Neo unchanged. The
first smoke test (`openclaw-smoke-001`) said `"workspace":
"%USERPROFILE%\\Desktop"`, so Neo wrote `openclaw-smoke-001-pi.txt`
there, as its skill told it to. That old file is still on the Desktop; it is
safe to delete.

## What changed

- `bridge_core.py`: when a task is routed to Neo, the bridge makes the folder
  `...\jarvis-bridge-tasks\<task-id>` and puts that path in the package's
  `workspace` before it drops the package into `openclaw-inbox`. Probe tasks
  sent from the bridge window get the same treatment.
- `gmail-bridge-config.json`: new setting
  `"work_dirs": {"openclaw": "%OPENCLAW_WORKSPACE%\\jarvis-bridge-tasks"}`.
  `%OPENCLAW_WORKSPACE%` means "wherever OpenClaw says Neo's workspace is". It
  is looked up on every task, so it follows OpenClaw if its workspace moves.
  A config without this setting gets the same value by default.
- The bridge window was restarted so the running bridge uses the new code.

Nothing else changed. Cursor tasks keep the workspace Jarvis sends (and the
approved-workspaces check). The email subjects, the task and result format,
the sender allowlist, the Gmail DKIM check, attachment handling, the inbox
watcher automation and the bridge-priority plugin are all as they were. The
watcher needed no change: it tells Neo to work inside the package's
`workspace`, which is now the task folder.

## What Jarvis sees

Neo copies the package's `workspace` into its result, so every
`[JARVIS-RESULT]` for a Neo task shows the task folder's full path.
Attachments are picked up from that folder exactly as before.

## Settings

- **Turn it off:** set `"work_dirs": {}` in `gmail-bridge-config.json`. Neo
  then works wherever Jarvis points again.
- **Use another folder:** set `"openclaw"` to any path, for example
  `"%USERPROFILE%\\neo-bridge-work"`. Each task still gets its own subfolder.
- **If OpenClaw's workspace cannot be found** (OpenClaw removed, or its
  workspace folder deleted), the task is still delivered with Jarvis's
  workspace, and the bridge log says why.

The bridge log shows each move, for example:
`Loop A: task abc-001 workspace -> %USERPROFILE%\.openclaw\workspace\jarvis-bridge-tasks\abc-001 (Jarvis sent %USERPROFILE%\Desktop)`
(the log prints the expanded folder).

## Checking it

- Unit tests: `python bridge_core_test.py` (the `work_dirs` and
  `OpenClaw workspace` lines).
- Smoke suite: `python smoke\run_smoke.py`. The `c-openclaw` scenario checks
  that a stub Neo writes its file into the task folder, nothing lands where
  Jarvis pointed, and the attachment still arrives byte for byte.
