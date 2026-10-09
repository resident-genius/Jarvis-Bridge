# Security Policy — Jarvis Bridge

## What this software does

Jarvis Bridge gives an **email account remote code execution on your Windows PC**.
A task email arrives, the bridge validates it, an AI agent on your PC runs it,
and the result is emailed back. Understand this before you install it:
**anyone who can send email from your paired mailbox can run commands on your computer.**

## Trust model

- **Pairing is the authority.** During setup you pair the bridge with one mailbox
  (a "type one code" step binds that mailbox to this install). Only task emails
  whose `From` exactly matches the paired mailbox are processed. Everything else
  is silently ignored — no reply, no notice.
- **Possession of the contract docs grants nothing.** The `contract/` folder teaches
  *any* agent the email format, but format is not authority. Only the paired
  mailbox can trigger execution. Keep these concepts separate.
- **No per-task approval in the default mode.** The stock install runs tasks from
  the paired mailbox without asking you first. If that mailbox is compromised,
  your PC is compromised. Protect the mailbox (strong password, 2FA) as if it
  were administrator access — because it is.

## What the bridge does NOT do

- It does not sandbox the executor. A task runs with your user account's
  privileges and can read and write your files.
- `do_not_touch` paths in a task email are **advisory text**, not enforced
  guardrails. A malicious or buggy task can ignore them.
- It does not encrypt task contents. Email is the transport; assume the provider
  can read it.

## Reporting a vulnerability

Open an issue on the GitHub repository. Do not include exploit code in the
public issue — describe the impact and we'll coordinate.

## Hardening checklist for your install

1. Use a dedicated mailbox for the bridge, not your primary email.
2. Enable 2FA on that mailbox.
3. Review the local task log regularly (see README "What did it just do").
4. Keep a kill switch handy: stopping the bridge service stops all execution.
   Uninstalling removes it entirely.
