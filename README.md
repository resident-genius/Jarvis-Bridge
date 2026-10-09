# Jarvis Bridge

A Gmail-bus bridge that lets AI agents send tasks to a Windows PC for execution via Cursor or OpenClaw.

You install it on a Windows PC, pair it with a mailbox by typing one code, and from then on any authorized agent can email that PC structured tasks — the PC runs them and emails back the results. No tunnels, no servers, no open ports.

## How it works

Two loops and a contract:

- **Loop A (mailbox watcher)** watches the paired mailbox for `[JARVIS-TASK]` emails, validates the JSON body against the contract, and routes each task to the on-PC executor.
- **Loop B (result sender)** runs the task, collects the report, and replies with a `[JARVIS-RESULT]` email containing a JSON report plus any attachments (screenshots, files).
- **The contract** (`contract/`) is the machine-readable spec: `CONTRACT.md` (plain-language), `task-schema.json` / `result-schema.json` (JSON Schema), and `examples/`. Any agent that reads these files can compose a valid task email with no prior knowledge.

Task emails go **from the paired mailbox to itself**. The bridge silently ignores anything from any other sender.

## Install

1. Download the latest release zip and extract it anywhere.
2. Right-click `Install-JarvisBridge.ps1` → **Run with PowerShell**.
   (Or: `powershell -ExecutionPolicy Bypass -File .\Install-JarvisBridge.ps1`)
3. Answer the wizard: your bridge mailbox, a machine label, which executor (`cursor` or `openclaw`).
4. Set the mailbox credentials (see below).
5. The installer runs a self-test. Green means the loop is live.

No admin rights needed — it installs under your user profile and starts at boot via a scheduled task.

**Requirements:** Windows 10/11 64-bit, Python 3.10+ (python.org installer, which includes tkinter for the GUI). No pip packages required — everything else is standard library. Optional: `pystray` + `pillow` for minimize-to-tray. The `cursor` executor needs the `cursor-agent` CLI on PATH.

Uninstall: run `Uninstall-JarvisBridge.ps1` from the install folder.

## Mailbox credentials

The bridge signs into your mailbox with IMAP. For Gmail:

1. Turn on 2-step verification on the Google account.
2. Create an app password at https://myaccount.google.com/apppasswords
3. Save it where the bridge expects it — see `bridge.config.json` (created by the wizard).

Use a **dedicated mailbox** for the bridge, not your primary email. See `SECURITY.md`.

## Sending a task

Compose an email **from the paired mailbox to itself**:

- **Subject:** `[JARVIS-TASK] my-task-001` (lowercase letters, digits, hyphens)
- **Body:** a JSON object following `contract/task-schema.json`. Required fields:

| Field | What it is |
|---|---|
| `contract_version` | `"1.0"` |
| `task_id` | Must match the subject |
| `workspace` | Absolute path on the PC where the agent works |
| `goal` | Full instructions. **Must end with:** `Report back with the exact result subject: [JARVIS-RESULT] <task-id>.` |
| `acceptance_criteria` | Array of discrete pass/fail statements — write them as the test, not wishes |
| `mode` | `"agent"` for anything needing shell/file access (almost everything) |
| `executor` | `"cursor"` (default) or `"openclaw"` |
| `report_back` | What content to put in the result |
| `do_not_touch` | Paths the agent must never modify (always include the bridge install dir) |
| `created` | ISO-8601 UTC timestamp |
| `timeout_minutes` | 1–720. Missing or out of range = rejected, no result ever comes |

See `contract/examples/` for a minimal and a full task. Validate your JSON against `contract/task-schema.json` before sending — the bridge rejects anything that fails it.

You'll get back a `[JARVIS-RESULT] my-task-001` email with a JSON report (`exit_code` 0 = all criteria passed) and attachments.

## What did it just do?

Every task, its result, and its attachments are logged locally where you can read them. The task workspace folders live under the `workspace_root` in your `bridge.config.json`. To revoke: stop the `JarvisBridge` scheduled task, or uninstall.

## Security

Read `SECURITY.md` before installing. The short version: **this is remote code execution by email**. Pairing is the only authority. Protect the mailbox like an admin password.

## License

MIT — see `LICENSE`.
