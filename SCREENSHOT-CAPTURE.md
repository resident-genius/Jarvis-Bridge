# Real desktop screenshots for Jarvis Bridge jobs

Bridge jobs run as LocalSystem in session 0. That session cannot see the
interactive desktop, so `CopyFromScreen` throws "The handle is invalid".
The bridge host needs a real monitor and an interactive user (local or RDP).

`{{INSTALL_DIR}}` below is the folder the bridge is installed in (the one
holding `bridge_core.py`).

## Pieces

- **Scheduled task name:** `JarvisBridge Screenshot`
- **Runs as:** the account that ran the register script (or
  `scheduled_tasks.run_as_user` in `bridge.config.json`), logon type
  Interactive ("Run only when user is logged on")
- **Capture script:** `{{INSTALL_DIR}}\jarvisbridge-screenshot-capture.ps1`
- **Incoming folder:** `{{INSTALL_DIR}}\proof\incoming\`
- **Helper Neo uses:** `%USERPROFILE%\.openclaw\workspace\skills\openclaw-bridge\bridge-proof.ps1`

## How a job captures

1. `bridge-proof.ps1` writes the wanted file stem to `proof\incoming\_request.txt`.
2. It starts the task with `schtasks /run /tn "JarvisBridge Screenshot"`.
3. The task runs the capture script in the interactive session, writes
   `<stem>.png` and `<stem>.done` into the incoming folder.
4. The helper copies that PNG into the task workspace as `<task-id>-proof.png`.
5. If nobody is logged on, the task fails, or no PNG appears within 30 seconds,
   the helper falls back to in-process `CopyFromScreen`, then to a rendered
   proof image. Jobs do not hard-fail on screenshots.

## Re-point for a different user

If the bridge host is used by a different Windows account than the one the
task was registered for:

```
schtasks /delete /tn "JarvisBridge Screenshot" /f
powershell -NoProfile -ExecutionPolicy Bypass -File {{INSTALL_DIR}}\register-jarvisbridge-screenshot-task.ps1 -User <that-username>
```

The register script is next to the capture script. Change `-User` only.
Do not change RDP or Windows logon settings.
