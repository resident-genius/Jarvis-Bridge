# Jarvis bridge autostart

The bridge starts on its own when you log on to Windows. Nothing needs to be
started by hand after a reboot.

## What is registered

A per-user **Scheduled Task**:

| | |
|---|---|
| Name | `JarvisBridge Autostart` |
| Where | Task Scheduler (`taskschd.msc`) > Task Scheduler Library (root folder) |
| Runs as | the user who registered it, only while that user is logged on (Interactive, not elevated) |
| Trigger | at that user's logon, 30 s delay (so the desktop and tray icon area are ready) |
| Action | `conhost.exe --headless powershell.exe ... -File <bridge folder>\Start-JarvisBridge.ps1 -IfNotRunning` |
| Settings | runs on battery, one task instance at a time, retries 3x at 1 min if it fails to start, 5 min limit on the launcher only |

`Start-JarvisBridge.ps1` is the canonical entry point. With `-IfNotRunning`
it leaves an already-running bridge alone. Otherwise it launches exactly one
`bridge_gui.py` with `pythonw.exe`, detached from the task, and checks that
only one instance is running. `bridge_gui.py` also holds its own
single-instance lock per config file. There is no console window: the
launcher runs headless, and `pythonw` has no console.

The older autostart, the `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
value `JarvisBridgeGUI`, was removed when the task was registered. That value
started `pythonw bridge_gui.py` directly and bypassed `Start-JarvisBridge.ps1`.

## Why a Scheduled Task (not the Startup folder or Run key)

- It runs `Start-JarvisBridge.ps1` with no console window, and it waits until
  the desktop is ready. Windows runs Startup-folder and Run-key entries only
  after its own startup delay, and **Settings > Apps > Startup** or Task
  Manager can switch them off silently.
- It keeps a run history (last run time and exit code), so you can check
  whether autostart worked. It retries if the launch fails.
- It never starts a second copy while one is running.
- It needs no administrator rights, account password, or extra software.

## Check / change / remove

```powershell
.\Register-JarvisBridgeAutostart.ps1 -Status       # show task + last run result (exit 0 = registered)
.\Register-JarvisBridgeAutostart.ps1               # register / update (re-run if the folder moves)
.\Register-JarvisBridgeAutostart.ps1 -Unregister   # remove autostart
```

The GUI checkbox **Start bridge when Windows starts** uses the same script.
To remove the task by hand, delete `JarvisBridge Autostart` in Task
Scheduler, or run `schtasks /Delete /TN "JarvisBridge Autostart" /F`.

## Cursor task runner watchdog

The task runner (`Invoke-JarvisTask.ps1` watching `cursor-inbox`) has its own
per-user Scheduled Task, `JarvisRunner Watchdog`, registered by
`Register-JarvisRunnerWatchdog.ps1`. It runs every 5 minutes while you are
logged on and 30 s after logon, headless, not elevated. Each run calls
`Restart-CursorRunner.ps1 -IfNotRunning`:

- a `powershell.exe` running `Invoke-JarvisTask.ps1 -Inbox <cursor-inbox>` is
  alive: nothing happens (`OK: runner alive`)
- none is alive: it starts one exactly like a plain `Restart-CursorRunner.ps1`
  (hidden, `-NoExit ... -AutoApprove`) and checks it is still up 3 s later
- none is alive but a `cursor-agent` from the dead runner is still working on a
  task that is still in the inbox (younger than 35 min): it waits, so that task
  is not run twice

It never stops a process. Every check is a line in `runner-watchdog.log` in
this folder (trimmed to the last 1000 lines past 256 KB).

The runner is an executor pool: up to 3 `cursor-agent` tasks run at once
(`-MaxParallel`), one per slot, and a slot is only used when a task is waiting.
Only one runner per inbox can run (a second one exits). The bridge window shows
each slot as idle or running a task id (from
`cursor-inbox\logs\executor-slots.json`). Slot starts and finishes are logged in
`cursor-inbox\logs\runner.log`. To load a new runner script while the runner is
busy with the task that changed it, use
`Restart-CursorRunner.ps1 -StopPid <runner pid> -AfterTask <task id>`.

Every task needs a positive `timeout_minutes` (max 720). There is no default:
the bridge rejects a task email without one at intake (Loop A) and mails a
`failed` `[JARVIS-RESULT]` naming the field; the runner refuses such a
package too. Nothing is started.

Each running task's worker writes a heartbeat every 30 s to
`cursor-inbox\logs\heartbeats\<task-id>.heartbeat.json` (outside the task
workspace) while its `cursor-agent` is alive. A heartbeat older than 5 min
means the worker is dead or frozen: the runner kills that worker, writes a
`failed` result (`heartbeat_stale: true`, log tail in `stderr`), which the
bridge mails, and frees the slot. The hard per-task timeout stays as the
backstop. The bridge window shows each slot's heartbeat age.

To load a new runner script while several slots are busy, without killing
any of them, arm `Restart-CursorRunner.ps1 -StopPid <runner pid> -WhenIdle`:
it replaces the runner the next time no task is running or queued.

Result emails: every send attempt is recorded first in `send-state.json`.
A result gets at most 5 send attempts (connection/login failures do not
count); after that, or after a Gmail 552 refusal, the bridge stops and mails
a small `<task-id>.send-failed-<hash>` failure notice instead.

```powershell
.\Register-JarvisRunnerWatchdog.ps1 -Status       # show task, last/next run
.\Register-JarvisRunnerWatchdog.ps1               # register / update
.\Register-JarvisRunnerWatchdog.ps1 -Unregister   # remove the watchdog
```

## Limitation

The bridge is a desktop GUI, so it starts at **logon**, not at the Windows
boot screen. After a reboot it comes up about 30 s after you log on. If the
machine should run the bridge with nobody at the keyboard, Windows automatic
sign-in has to be turned on separately. That was not changed here.
