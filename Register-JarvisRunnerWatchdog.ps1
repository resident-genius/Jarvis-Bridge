<#
.SYNOPSIS
  Registers, checks or removes the Cursor task runner watchdog.
.DESCRIPTION
  The watchdog is a per-user Scheduled Task named "JarvisRunner Watchdog" in
  the root of the Task Scheduler Library (taskschd.msc). It runs
  Restart-CursorRunner.ps1 -IfNotRunning from this folder, with no console
  window (conhost --headless):
    - every -IntervalMinutes (default 5) while the user is logged on, and
    - at the user's logon (after -LogonDelaySeconds), which covers reboots.
  Each run only checks for a powershell.exe running Invoke-JarvisTask.ps1 on
  the inbox and starts one if none is alive. It never stops a running runner.
  Checks are logged to runner-watchdog.log in this folder.
  No administrator rights are needed; the task runs as the current user.
  Re-run after moving the bridge folder (the task stores this folder's path).
.EXAMPLE
  .\Register-JarvisRunnerWatchdog.ps1               # register / update
  .\Register-JarvisRunnerWatchdog.ps1 -Status       # exit 0 = registered
  .\Register-JarvisRunnerWatchdog.ps1 -Unregister   # remove the watchdog
.NOTES
  Manual removal: Task Scheduler > Task Scheduler Library > delete
  "JarvisRunner Watchdog", or: schtasks /Delete /TN "JarvisRunner Watchdog" /F
  -TaskName/-Inbox/-AllowFile/-LogFile exist so a throwaway copy can watch a
  test inbox; the defaults are the real runner.
#>
param(
  [switch]$Unregister,
  [switch]$Status,
  [string]$TaskName = 'JarvisRunner Watchdog',
  [int]$IntervalMinutes = 5,
  [int]$LogonDelaySeconds = 30,
  [string]$Inbox = '',
  [string]$AllowFile = '',
  [string]$LogFile = ''
)
$ErrorActionPreference = 'Stop'
$bridgeSettingsScript = Join-Path $PSScriptRoot 'BridgeSettings.ps1'
try { if (Test-Path -LiteralPath $bridgeSettingsScript) { . $bridgeSettingsScript } } catch { }
if (-not (Get-Command Get-BridgeSetting -ErrorAction SilentlyContinue)) {
  function Get-BridgeSetting([string]$Key, $Default) { return ,$Default }
  function Expand-BridgePath([string]$Value) { [Environment]::ExpandEnvironmentVariables($Value) }
}
foreach ($bsParam in @(
    @('TaskName', 'scheduled_tasks.runner_watchdog.name'),
    @('IntervalMinutes', 'scheduled_tasks.runner_watchdog.interval_minutes'),
    @('LogonDelaySeconds', 'scheduled_tasks.runner_watchdog.logon_delay_seconds'))) {
  if ($PSBoundParameters.ContainsKey($bsParam[0])) { continue }
  try { Set-Variable -Name $bsParam[0] -Value (Get-BridgeSetting $bsParam[1] (Get-Variable -Name $bsParam[0] -ValueOnly)) } catch { }
}
$execLimitMinutes = Get-BridgeSetting 'scheduled_tasks.runner_watchdog.execution_time_limit_minutes' 5
$restartCount = Get-BridgeSetting 'scheduled_tasks.runner_watchdog.restart_count' 3
$restartIntervalMinutes = Get-BridgeSetting 'scheduled_tasks.runner_watchdog.restart_interval_minutes' 1
$restartScript = Join-Path $PSScriptRoot 'Restart-CursorRunner.ps1'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

function Get-Task { Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue }

function Show-Task($t) {
  $a = $t.Actions[0]
  Write-Host "Task      : $($t.TaskPath)$($t.TaskName)  (state: $($t.State))"
  Write-Host "Runs as   : $($t.Principal.UserId), $($t.Principal.LogonType), $($t.Principal.RunLevel)"
  foreach ($tr in $t.Triggers) {
    if ($tr.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger') {
      Write-Host "Trigger   : at logon of $($tr.UserId), delay $($tr.Delay)"
    } else {
      $dur = if ($tr.Repetition.Duration) { $tr.Repetition.Duration } else { 'indefinitely' }
      Write-Host "Trigger   : from $($tr.StartBoundary), repeat every $($tr.Repetition.Interval) $dur"
    }
  }
  Write-Host "Command   : $($a.Execute) $($a.Arguments)"
  Write-Host "Start in  : $($a.WorkingDirectory)"
}

if ($Status) {
  $t = Get-Task
  if (-not $t) { Write-Host "Watchdog task '$TaskName' is not registered."; exit 1 }
  Show-Task $t
  $i = $t | Get-ScheduledTaskInfo
  Write-Host "Last run  : $($i.LastRunTime)  result 0x$('{0:X}' -f $i.LastTaskResult)"
  Write-Host "Next run  : $($i.NextRunTime)"
  exit 0
}

if ($Unregister) {
  if (Get-Task) {
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
    Write-Host "Removed watchdog task '$TaskName'."
  } else {
    Write-Host "Watchdog task '$TaskName' was not registered."
  }
  exit 0
}

if (-not (Test-Path $restartScript)) { throw "Not found: $restartScript" }
if ($IntervalMinutes -lt 1) { throw '-IntervalMinutes must be at least 1.' }
$sys32 = Join-Path $env:SystemRoot 'System32'
$conhost = Expand-BridgePath (Get-BridgeSetting 'paths.conhost' (Join-Path $sys32 'conhost.exe'))
$powershell = Expand-BridgePath (Get-BridgeSetting 'paths.windows_powershell' (Join-Path $sys32 'WindowsPowerShell\v1.0\powershell.exe'))
$arguments = ('--headless "{0}" -NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
  '-WindowStyle Hidden -File "{1}" -IfNotRunning') -f $powershell, $restartScript
foreach ($opt in @(@('Inbox', $Inbox), @('AllowFile', $AllowFile), @('LogFile', $LogFile))) {
  if ($opt[1]) { $arguments += ' -{0} "{1}"' -f $opt[0], $opt[1].TrimEnd('\') }
}

$action = New-ScheduledTaskAction -Execute $conhost -Argument $arguments -WorkingDirectory $PSScriptRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$logon.Delay = 'PT{0}S' -f [Math]::Max(0, $LogonDelaySeconds)
# Starts at today's midnight so checks land on round clock times (:00, :05, ...).
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
# The task itself is only the short-lived check; the runner is started with
# Start-Process and is not part of the task, so the time limit never stops it.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes $execLimitMinutes) `
  -RestartCount $restartCount -RestartInterval (New-TimeSpan -Minutes $restartIntervalMinutes) -StartWhenAvailable
$desc = "Every $IntervalMinutes min and at logon: starts the Cursor task runner (Invoke-JarvisTask.ps1) " +
  "only if it is not running, via $restartScript -IfNotRunning. Never stops a running runner. " +
  "Remove with Register-JarvisRunnerWatchdog.ps1 -Unregister."

$null = Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $action -Trigger @($logon, $repeat) `
  -Principal $principal -Settings $settings -Description $desc -Force
$t = Get-Task
if (-not $t) { throw "Task '$TaskName' was not registered." }
Write-Host "Registered watchdog task."
Show-Task $t
