<#
.SYNOPSIS
  Registers, checks or removes the Jarvis bridge's Windows logon autostart.
.DESCRIPTION
  The autostart is a per-user Scheduled Task named "JarvisBridge Autostart"
  in the root of the Task Scheduler Library (taskschd.msc). At the current
  user's logon (after a short delay, so the desktop and tray are up) it runs
  Start-JarvisBridge.ps1 -IfNotRunning from this folder, in the user's
  desktop session, with no console window (conhost --headless). That script
  launches exactly one bridge_gui.py, or leaves a running one alone.

  Registering also deletes the older HKCU ...\CurrentVersion\Run value
  "JarvisBridgeGUI", so only one autostart mechanism exists.
  No administrator rights are needed; the task runs as the current user.
  Re-run after moving the bridge folder (the task stores this folder's path).
.EXAMPLE
  .\Register-JarvisBridgeAutostart.ps1               # register / update
  .\Register-JarvisBridgeAutostart.ps1 -Status       # exit 0 = registered
  .\Register-JarvisBridgeAutostart.ps1 -Unregister   # remove autostart
.NOTES
  Manual removal: Task Scheduler > Task Scheduler Library > delete
  "JarvisBridge Autostart", or: schtasks /Delete /TN "JarvisBridge Autostart" /F
#>
param(
  [switch]$Unregister,
  [switch]$Status,
  [int]$DelaySeconds = 30
)
$ErrorActionPreference = 'Stop'
$bridgeSettingsScript = Join-Path $PSScriptRoot 'BridgeSettings.ps1'
try { if (Test-Path -LiteralPath $bridgeSettingsScript) { . $bridgeSettingsScript } } catch { }
if (-not (Get-Command Get-BridgeSetting -ErrorAction SilentlyContinue)) {
  function Get-BridgeSetting([string]$Key, $Default) { return ,$Default }
  function Expand-BridgePath([string]$Value) { [Environment]::ExpandEnvironmentVariables($Value) }
}
if (-not $PSBoundParameters.ContainsKey('DelaySeconds')) {
  try { $DelaySeconds = Get-BridgeSetting 'scheduled_tasks.autostart.logon_delay_seconds' $DelaySeconds } catch { }
}
# bridge_gui.py reads the same scheduled_tasks.autostart keys.
$TaskName = Get-BridgeSetting 'scheduled_tasks.autostart.name' 'JarvisBridge Autostart'
$LegacyRunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$LegacyRunValue = Get-BridgeSetting 'scheduled_tasks.autostart.legacy_run_value' 'JarvisBridgeGUI'
$execLimitMinutes = Get-BridgeSetting 'scheduled_tasks.autostart.execution_time_limit_minutes' 5
$restartCount = Get-BridgeSetting 'scheduled_tasks.autostart.restart_count' 3
$restartIntervalMinutes = Get-BridgeSetting 'scheduled_tasks.autostart.restart_interval_minutes' 1

$startScript = Join-Path $PSScriptRoot 'Start-JarvisBridge.ps1'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

function Get-Task { Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue }

function Remove-LegacyRunValue {
  if (Get-ItemProperty -Path $LegacyRunKey -Name $LegacyRunValue -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $LegacyRunKey -Name $LegacyRunValue
    Write-Host "Removed legacy autostart: HKCU Run value '$LegacyRunValue'."
  }
}

function Show-Task($t) {
  $a = $t.Actions[0]
  Write-Host "Task      : $($t.TaskPath)$($t.TaskName)  (state: $($t.State))"
  Write-Host "Runs as   : $($t.Principal.UserId), $($t.Principal.LogonType), $($t.Principal.RunLevel)"
  Write-Host "Trigger   : at logon of $($t.Triggers[0].UserId), delay $($t.Triggers[0].Delay)"
  Write-Host "Command   : $($a.Execute) $($a.Arguments)"
  Write-Host "Start in  : $($a.WorkingDirectory)"
}

if ($Status) {
  $t = Get-Task
  $legacy = [bool](Get-ItemProperty -Path $LegacyRunKey -Name $LegacyRunValue -ErrorAction SilentlyContinue)
  if ($legacy) { Write-Host "Legacy HKCU Run value '$LegacyRunValue' is present." }
  if (-not $t) { Write-Host "Autostart task '$TaskName' is not registered."; exit 1 }
  Show-Task $t
  $i = $t | Get-ScheduledTaskInfo
  Write-Host "Last run  : $($i.LastRunTime)  result 0x$('{0:X}' -f $i.LastTaskResult)"
  exit 0
}

if ($Unregister) {
  if (Get-Task) {
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
    Write-Host "Removed autostart task '$TaskName'."
  } else {
    Write-Host "Autostart task '$TaskName' was not registered."
  }
  Remove-LegacyRunValue
  exit 0
}

if (-not (Test-Path $startScript)) { throw "Not found: $startScript" }
$sys32 = Join-Path $env:SystemRoot 'System32'
$conhost = Expand-BridgePath (Get-BridgeSetting 'paths.conhost' (Join-Path $sys32 'conhost.exe'))
$powershell = Expand-BridgePath (Get-BridgeSetting 'paths.windows_powershell' (Join-Path $sys32 'WindowsPowerShell\v1.0\powershell.exe'))
$arguments = ('--headless "{0}" -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
  '-WindowStyle Hidden -File "{1}" -IfNotRunning') -f $powershell, $startScript

$action = New-ScheduledTaskAction -Execute $conhost -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT{0}S' -f [Math]::Max(0, $DelaySeconds)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
# The task itself is only the short-lived launcher; the bridge is not its
# child (Start-JarvisBridge.ps1 detaches it), so the time limit never stops it.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes $execLimitMinutes) `
  -RestartCount $restartCount -RestartInterval (New-TimeSpan -Minutes $restartIntervalMinutes) -StartWhenAvailable
$desc = "Starts the Jarvis bridge (bridge_gui.py, one instance) at logon via " +
  "$startScript -IfNotRunning. Remove with Register-JarvisBridgeAutostart.ps1 -Unregister."

$null = Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Description $desc -Force
Remove-LegacyRunValue
$t = Get-Task
if (-not $t) { throw "Task '$TaskName' was not registered." }
Write-Host "Registered autostart task."
Show-Task $t
