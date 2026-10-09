# Jarvis Bridge — Task Email Contract
**Contract version:** 1.0 · 2026-10-08
**Status:** Extracted from the running production system. This document is authoritative: if a future agent cannot compose a working task email from these files alone, these files are incomplete.

---

## 1. What this is

The Jarvis Bridge lets an authorized sender assign work to a Windows PC by email. The sender writes a **task email**; the bridge validates it, runs it with an **executor** (an AI agent on the PC), and replies with a **result email**. This contract defines both emails exactly.

## 2. Transport

- Task emails are sent **from the paired mailbox to itself** (sender == recipient). The mailbox address is set at install time (the "pairing"); it is never hardcoded in this contract.
- The bridge **ignores** any message whose `From` is not the paired mailbox. No reply, no notice — silence.
- Subject line formats:
  - `[JARVIS-TASK] <task-id>` — runs on the bridge's own machine.
  - `[JARVIS-TASK:D01] <task-id>` — routes to the webserver spoke.
  - `[JARVIS-TASK:D02] <task-id>` — routes to the GPU workhorse spoke.
  - (Routing tags are install-config; a stock install only honors the untagged form.)
- Result emails use the subject `[JARVIS-RESULT] <task-id>` and go to the same mailbox.

## 3. Task email body

The body MUST be a single JSON object. A body that is not valid JSON is **rejected at intake** with a skip notice; it never runs.

### Required fields

| Field | Type | Rules |
|---|---|---|
| `contract_version` | string | `"1.0"`. The bridge rejects unknown major versions. |
| `task_id` | string | Lowercase letters, digits, hyphens. Must be unique per sender. The result email's subject and body must echo it exactly. |
| `workspace` | string | Absolute path on the target PC where the executor works, e.g. `C:\Users\<user>\.openclaw\workspace\jarvis-bridge-tasks\<task-id>`. The executor must not write outside it except where the goal explicitly says so. |
| `goal` | string | Full instructions for the executor, plain language. MUST end with the exact sentence: `Report back with the exact result subject: [JARVIS-RESULT] <task-id>.` (with the real task id substituted). |
| `acceptance_criteria` | string[] | One or more discrete, verifiable pass/fail statements. Not one blob. The executor must be able to check each one; strict criteria are the quality mechanism — write them as the test, not as wishes. |
| `mode` | string | `"agent"` (full shell + file access), `"plan"`, or `"ask"`. **Use `"agent"` for any task that needs shell commands or file writes.** Any other value runs restricted (no shell, no external file edits) and the task will silently fail to do real work. |
| `report_back` | string | WHAT content to put in the result (facts, numbers, file names). Never put email-sending instructions here — the bridge owns delivery. |
| `do_not_touch` | string[] | Paths the executor must never modify. Always include the bridge installation directory itself. |
| `created` | string | ISO-8601 UTC timestamp of composition, e.g. `2026-10-08T14:26:41Z`. |
| `timeout_minutes` | integer | 1–720. **Missing or out-of-range → rejected at intake** with a `missing-timeout` skip notice. This is a hard backstop; pick generously for long renders. |

### Optional fields

| Field | Type | Rules |
|---|---|---|
| `executor` | string | `"cursor"` (default) or `"openclaw"`. Unknown values → skip notice, never silent. |
| `screenshot` | boolean | Default `true`. The executor must attach a PNG screenshot as proof (real desktop, not mockups). Set `false` only for pure headless probes. |

### Intake validation order (first failure wins, all produce a skip notice except wrong-sender)

1. `From` == paired mailbox, else silent ignore.
2. Body parses as JSON, else skip (`body-not-json`).
3. All required fields present and correctly typed, else skip (`bad-contract`).
4. `timeout_minutes` present and 1–720, else skip (`missing-timeout`).
5. `executor` known, else skip (`unknown-executor`).
6. `goal` ends with the exact result-subject sentence, else skip (`bad-result-subject`).

## 4. Execution rules the sender should know

- **Tasks can run concurrently.** Never promise ordering across task emails.
- **Screenshot proof is the norm.** Every task should produce a PNG proving what it did; a task with no evidence is a task that didn't happen.
- **Synthetic test data only** unless the goal explicitly authorizes otherwise — never personal photos or private files unless the paired owner asked for them.
- **Dead-worker detection:** the executor should touch a heartbeat file ~every 60s; the bridge treats a heartbeat stale >5 min as failed, with `timeout_minutes` as the final backstop.
- **Failure is data.** If the executor cannot meet the acceptance criteria, it reports failure plainly with the reason. A failed result is still a result.

## 5. Result email

- Subject: exactly `[JARVIS-RESULT] <task-id>`.
- Body: a single JSON object:

| Field | Type | Rules |
|---|---|---|
| `task_id` | string | Echoes the task email's id exactly. |
| `exit_code` | integer | `0` = all acceptance criteria passed. Anything else = failed/partial. |
| `acceptance_criteria` | array | Each criterion with its pass/fail outcome. |
| `jarvis_report` | string | Plain-language summary: what was done, what was not done and why. No jargon, no raw dumps. |
| `stdout` | string | Executor's standard output (may be empty). |
| `stderr` | string | Executor's standard error (may be empty). |
| `attachments` | string[] | Basenames of files actually attached to this email. |
| `attachments_skipped` | array | `[{"path": "...", "reason": "..."}]` for files that could not be attached. |

- Attachments ride on the email itself. Keep them modest; huge files should be summarized with a path instead.
- **Undeliverable results** (mailbox refuses them) age out after 24h. This is normal, not an error.

## 6. Rejection examples (do not do these)

- Plain-text body ("please restart the PC") → skipped, `body-not-json`. The bridge only reads JSON.
- JSON without `timeout_minutes` → skipped, `missing-timeout`. No result will ever come.
- `"mode": "task"` expecting shell access → runs restricted, task does nothing useful. Always `"agent"` for real work.
- `goal` ending without the result-subject sentence → skipped, `bad-result-subject`. The sentence is how the result gets routed home.
- Sending from any other mailbox → silently ignored. The pairing is the authority, not the content.

## 7. Versioning

- `contract_version` is `major.minor`. The bridge accepts any `1.x`; a `2.0` task on a `1.x` bridge gets a skip notice naming the mismatch.
- Changes to this file bump the version and are recorded below.

### Changelog
- **1.0 (2026-10-08):** Initial extraction from the production system.
