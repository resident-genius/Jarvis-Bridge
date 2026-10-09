<#
.SYNOPSIS
  Replace the cursor inbox runner with a fresh Invoke-JarvisTask.ps1 process
  so script changes on disk actually load.
.DESCRIPTION
  -IfNotRunning is the watchdog mode used by the "JarvisRunner Watchdog"
  scheduled task (Register-JarvisRunnerWatchdog.ps1). It starts a runner only
  when no powershell.exe is running Invoke-JarvisTask.ps1 on -Inbox, and it
  never stops anything. Every check is appended to -LogFile.
  It also holds off while a cursor-agent started by a dead runner is still
  working on a task that is still in the inbox (younger than
  -OrphanGraceMinutes), because a new runner would pick that task up again.
  -AfterTask is the handoff mode for upgrading the runner from a task that
  the runner itself is executing: it waits until that task's package lands in
  <inbox>\done (its result is written first), then stops -StopPid and every
  process it started since (a next task it may have begun launching), starts
  a fresh runner and checks that it reports executor slots. Logged to -LogFile.
  -WhenIdle is the handoff mode for a busy pool: it waits until -StopPid
  reports no running and no queued task (twice in a row, and no package in
  the inbox), so no in-flight task of any slot is killed, then replaces it
  the same way. Logged to -LogFile.
.EXAMPLE
  .\Restart-CursorRunner.ps1 -StopPid 1234   # replace runner PID 1234
  .\Restart-CursorRunner.ps1 -IfNotRunning   # start one only if none is alive
  .\Restart-CursorRunner.ps1 -StopPid 1234 -AfterTask my-task-001
  .\Restart-CursorRunner.ps1 -StopPid 1234 -WhenIdle
#>
param(
  [int[]]$StopPid = @(),
  [switch]$IfNotRunning,
  [string]$AfterTask = '',
  [int]$AfterTaskMaxHours = 13,
  [switch]$WhenIdle,
  [int]$WhenIdleMaxHours = 48,
  [string]$Inbox = (Join-Path $env:USERPROFILE 'cursor-inbox'),
  [string]$AllowFile = '',
  [string]$LogFile = '',
  [int]$OrphanGraceMinutes = 35
)
$ErrorActionPreference = 'Stop'
# Settings from bridge.config.json (BridgeSettings.ps1). A parameter given on
# the command line wins; every value falls back to the literal default here.
$bridgeSettingsScript = Join-Path $PSScriptRoot 'BridgeSettings.ps1'
try { if (Test-Path -LiteralPath $bridgeSettingsScript) { . $bridgeSettingsScript } } catch { }
if (-not (Get-Command Get-BridgeSetting -ErrorAction SilentlyContinue)) {
  function Get-BridgeSetting([string]$Key, $Default) { return ,$Default }
  function Get-BridgeSettingLeaf([string]$Key, [string]$Default) { return $Default }
  function Get-BridgeInboxRelative([string]$Key, [string]$Default) { return $Default }
  function Expand-BridgePath([string]$Value) { [Environment]::ExpandEnvironmentVariables($Value) }
}
if (-not $PSBoundParameters.ContainsKey('Inbox')) { $Inbox = Expand-BridgePath (Get-BridgeSetting 'paths.inboxes.cursor' $Inbox) }
foreach ($bsParam in @(
    @('AfterTaskMaxHours', 'timeouts.watchdog_after_task_max_hours'),
    @('WhenIdleMaxHours', 'timeouts.watchdog_when_idle_max_hours'),
    @('OrphanGraceMinutes', 'timeouts.watchdog_orphan_grace_minutes'))) {
  if ($PSBoundParameters.ContainsKey($bsParam[0])) { continue }
  try { Set-Variable -Name $bsParam[0] -Value (Get-BridgeSetting $bsParam[1] (Get-Variable -Name $bsParam[0] -ValueOnly)) } catch { }
}
$taskSuffix = Get-BridgeSetting 'executors.task_file_suffix' '.jarvis-task.json'
$resultSuffix = Get-BridgeSetting 'executors.result_file_suffix' '.jarvis-result.json'
$cursorCli = Get-BridgeSetting 'executors.cursor.cli' 'cursor-agent'
$autoApprove = Get-BridgeSetting 'executors.cursor.auto_approve' $true
$windowsPowerShell = Expand-BridgePath (Get-BridgeSetting 'paths.windows_powershell' "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe")
$bsSubdirs = [string[]](Get-BridgeSetting 'paths.inbox_subdirs' @('done', 'logs'))
$doneDirName = if ($bsSubdirs.Count -ge 2) { $bsSubdirs[0] } else { 'done' }
$slotRel = Get-BridgeInboxRelative 'paths.executor_slots_file' 'logs\executor-slots.json'
$slotsStaleSeconds = Get-BridgeSetting 'timeouts.executor_slots_stale_seconds' 20
$logMaxBytes = [long](Get-BridgeSetting 'limits.watchdog_log_max_bytes' 262144)
$logKeepLines = Get-BridgeSetting 'limits.watchdog_log_keep_lines' 1000
$afterTaskPollMs = Get-BridgeSetting 'timeouts.watchdog_after_task_poll_ms' 50
$whenIdlePollSeconds = Get-BridgeSetting 'timeouts.watchdog_when_idle_poll_seconds' 3
$treeRoundMs = Get-BridgeSetting 'timeouts.watchdog_process_tree_round_ms' 750
$restartSettleMs = Get-BridgeSetting 'timeouts.watchdog_restart_settle_ms' 800
$handoffVerifySeconds = Get-BridgeSetting 'timeouts.watchdog_handoff_verify_seconds' 30
$startVerifySeconds = Get-BridgeSetting 'timeouts.watchdog_start_verify_seconds' 3
$watchdogMutexPrefix = Get-BridgeSetting 'scheduled_tasks.mutex_prefixes.runner_watchdog' 'Local\JarvisRunnerWatchdog-'
$runner = Join-Path $PSScriptRoot 'Invoke-JarvisTask.ps1'
$inbox = $Inbox.TrimEnd('\')
$allow = if ($AllowFile) { $AllowFile } else { Join-Path $inbox (Get-BridgeSettingLeaf 'paths.allowed_workspaces_file' 'allowed-workspaces.txt') }
if (-not $LogFile) { $LogFile = Join-Path $PSScriptRoot (Get-BridgeSettingLeaf 'paths.watchdog_log' 'runner-watchdog.log') }

function Format-Arg($s) { if ($s -match '\s') { '"{0}"' -f $s } else { $s } }

function Start-Runner {
  $arg = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit',
    '-File', (Format-Arg $runner),
    '-Inbox', (Format-Arg $inbox),
    '-AllowFile', (Format-Arg $allow)
  )
  if ($autoApprove) { $arg += '-AutoApprove' }
  Start-Process -FilePath $windowsPowerShell `
    -ArgumentList $arg -PassThru -WindowStyle Hidden
}

function Get-RunnerProcesses {
  $inboxRx = '(?i)\s-Inbox\s+"?' + [regex]::Escape($inbox) + '\\?"?(\s|$)'
  @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.CommandLine -and
    $_.CommandLine -match '(?i)\s-File\s+"?[^"]*Invoke-JarvisTask\.ps1"?(\s|$)' -and
    $_.CommandLine -match $inboxRx
  })
}

function Get-OrphanAgents {
  $workspaces = @(Get-ChildItem (Join-Path $inbox ('*' + $taskSuffix)) -ErrorAction SilentlyContinue |
    ForEach-Object { try { [string](Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json).workspace } catch { } } |
    Where-Object { $_ } | ForEach-Object { $_.Trim().TrimEnd('\') })
  if ($workspaces.Count -eq 0) { return @() }
  $since = (Get-Date).AddMinutes(-$OrphanGraceMinutes)
  @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object {
    $cl = $_.CommandLine
    $cl -and $cl -match ('(?i)' + [regex]::Escape($cursorCli)) -and $cl -match '\s-p\s' -and $_.CreationDate -gt $since -and
    @($workspaces | Where-Object {
      $cl.IndexOf("--workspace $_ ", [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
      $cl.IndexOf("--workspace `"$_`"", [StringComparison]::OrdinalIgnoreCase) -ge 0
    }).Count -gt 0
  })
}

function Write-Log($msg) {
  $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $inbox, $msg
  Write-Host $line
  try {
    if ((Test-Path -LiteralPath $LogFile) -and (Get-Item -LiteralPath $LogFile).Length -gt $logMaxBytes) {
      $keep = @(Get-Content -LiteralPath $LogFile -Tail $logKeepLines)
      Set-Content -LiteralPath $LogFile -Value $keep -Encoding UTF8
    }
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
  } catch { Write-Host "(could not write ${LogFile}: $_)" }
}

function Stop-ProcessTree([int]$rootPid, [datetime]$since) {
  # The root may already be gone; its children still carry its PID as parent.
  $killed = New-Object 'System.Collections.Generic.HashSet[int]'
  $null = $killed.Add($rootPid)
  for ($round = 0; $round -lt 4; $round++) {
    $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    do {
      $more = @($procs | Where-Object { $killed.Contains([int]$_.ParentProcessId) -and -not $killed.Contains([int]$_.ProcessId) -and $_.CreationDate -ge $since })
      foreach ($p in $more) {
        $null = $killed.Add([int]$p.ProcessId)
        Write-Log "HANDOFF: stopping PID $($p.ProcessId) ($($p.Name)) started by the old runner."
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
      }
    } while ($more.Count -gt 0)
    Start-Sleep -Milliseconds $treeRoundMs
  }
}

if ($WhenIdle) {
  if ($IfNotRunning -or $AfterTask -or $StopPid.Count -ne 1) { throw '-WhenIdle needs exactly one -StopPid and no -IfNotRunning / -AfterTask.' }
  $old = $StopPid[0]
  $oldProc = Get-Process -Id $old -ErrorAction SilentlyContinue
  $oldStart = if ($oldProc) { $oldProc.StartTime } else { Get-Date }
  $slotFile = Join-Path $inbox $slotRel
  $deadline = (Get-Date).AddHours($WhenIdleMaxHours)
  Write-Log "ARMED: will replace runner PID $old the next time it has no running or queued task."
  $idleSeen = 0
  while ($true) {
    if (-not (Get-Process -Id $old -ErrorAction SilentlyContinue)) { Write-Log "WHENIDLE: runner PID $old is already gone; nothing to replace (the watchdog starts the current script)."; exit 0 }
    if ((Get-Date) -gt $deadline) { Write-Log "GIVE UP: runner PID $old was never idle within $WhenIdleMaxHours h; left as is."; exit 1 }
    $idle = $false
    try {
      $s = Get-Content -LiteralPath $slotFile -Raw | ConvertFrom-Json
      $fresh = ([DateTimeOffset]::Now.ToUnixTimeSeconds() - [long]$s.updated_unix) -le $slotsStaleSeconds
      $idle = ([int]$s.runner_pid -eq $old) -and $fresh -and -not $s.stopped -and ([int]$s.running -eq 0) -and ([int]$s.queued -eq 0) -and
        -not (Test-Path (Join-Path $inbox ('*' + $taskSuffix)))
    } catch { }
    if ($idle) { $idleSeen++ } else { $idleSeen = 0 }
    if ($idleSeen -ge 2 -and -not (Test-Path (Join-Path $inbox ('*' + $taskSuffix)))) { break }
    Start-Sleep -Seconds $whenIdlePollSeconds
  }
  Stop-Process -Id $old -Force -ErrorAction SilentlyContinue
  Write-Log "HANDOFF: runner PID $old idle (no running/queued task); stopped it."
  Stop-ProcessTree $old $oldStart
  $p = Start-Runner
  $ok = $false
  for ($i = 0; $i -lt $handoffVerifySeconds -and -not $ok; $i++) {
    Start-Sleep -Seconds 1
    try {
      $s = Get-Content -LiteralPath $slotFile -Raw | ConvertFrom-Json
      $ok = ([int]$s.runner_pid -eq $p.Id) -and -not $s.stopped -and @(Get-RunnerProcesses | Where-Object { $_.ProcessId -eq $p.Id }).Count -eq 1
    } catch { }
  }
  if ($ok) { Write-Log "HANDOFF OK: new runner PID $($p.Id) is alive and reporting $($s.max_slots) executor slot(s), heartbeat limit $($s.heartbeat_stale_seconds) s."; exit 0 }
  Write-Log "HANDOFF FAIL: new runner PID $($p.Id) did not report executor slots within $handoffVerifySeconds s; the watchdog will start one if it is gone."
  exit 1
}

if ($AfterTask) {
  if ($IfNotRunning -or $StopPid.Count -ne 1) { throw '-AfterTask needs exactly one -StopPid and no -IfNotRunning.' }
  $old = $StopPid[0]
  $taskFile = Join-Path $inbox "$AfterTask$taskSuffix"
  $doneFile = Join-Path $inbox "$doneDirName\$AfterTask$taskSuffix"
  $resultFile = Join-Path $inbox "$AfterTask$resultSuffix"
  $deadline = (Get-Date).AddHours($AfterTaskMaxHours)
  $oldProc = Get-Process -Id $old -ErrorAction SilentlyContinue
  $oldStart = if ($oldProc) { $oldProc.StartTime } else { Get-Date }
  Write-Log "ARMED: will replace runner PID $old right after task $AfterTask finishes."
  while ($true) {
    $done = [IO.File]::Exists($doneFile) -and -not [IO.File]::Exists($taskFile)
    $gone = -not (Get-Process -Id $old -ErrorAction SilentlyContinue)
    if ($done -or $gone) { break }
    if ((Get-Date) -gt $deadline) { Write-Log "GIVE UP: task $AfterTask not finished after $AfterTaskMaxHours h; runner PID $old left as is."; exit 1 }
    Start-Sleep -Milliseconds $afterTaskPollMs
  }
  Stop-Process -Id $old -Force -ErrorAction SilentlyContinue
  Write-Log ("HANDOFF: task $AfterTask " + $(if ($done) { 'finished' } else { 'runner exited on its own' }) + "; stopped runner PID $old.")
  Stop-ProcessTree $old $oldStart
  if ([IO.File]::Exists($taskFile) -and [IO.File]::Exists($resultFile)) {
    Move-Item -LiteralPath $taskFile -Destination $doneFile -Force
    Write-Log "HANDOFF: moved finished package $AfterTask to done (old runner stopped before moving it)."
  }
  $orphans = @(Get-OrphanAgents)
  if ($orphans.Count -gt 0) { Write-Log "HANDOFF: cursor-agent PID $(($orphans | ForEach-Object ProcessId) -join ', ') still works on an inbox task; the new runner holds that task until it exits." }
  $p = Start-Runner
  $slotFile = Join-Path $inbox $slotRel
  $ok = $false
  for ($i = 0; $i -lt $handoffVerifySeconds -and -not $ok; $i++) {
    Start-Sleep -Seconds 1
    try {
      $s = Get-Content -LiteralPath $slotFile -Raw | ConvertFrom-Json
      $ok = ([int]$s.runner_pid -eq $p.Id) -and -not $s.stopped -and @(Get-RunnerProcesses | Where-Object { $_.ProcessId -eq $p.Id }).Count -eq 1
    } catch { }
  }
  if ($ok) { Write-Log "HANDOFF OK: new runner PID $($p.Id) is alive and reporting $($s.max_slots) executor slot(s): $($s.running) running, $($s.queued) queued." ; exit 0 }
  Write-Log "HANDOFF FAIL: new runner PID $($p.Id) did not report executor slots within $handoffVerifySeconds s; the watchdog will start one if it is gone."
  exit 1
}

if (-not $IfNotRunning) {
  foreach ($id in $StopPid) {
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
      Write-Host "Stopping PID $id"
      Stop-Process -Id $id -Force
    }
  }
  Start-Sleep -Milliseconds $restartSettleMs
  $p = Start-Runner
  Write-Host "Started runner PID $($p.Id)"
  exit 0
}

if ($StopPid.Count -gt 0) { throw '-IfNotRunning never stops processes; do not combine it with -StopPid.' }
$code = 0
$mutexName = $watchdogMutexPrefix + ($inbox.ToLower() -replace '[^a-z0-9]', '_')
$mutex = [System.Threading.Mutex]::new($false, $mutexName)
if (-not $mutex.WaitOne(0)) { Write-Log 'SKIP: another watchdog check is already running.'; exit 0 }
try {
  # @() at the call site: a function returning one CimInstance unrolls to a
  # scalar whose .Count is $null, which would look like "no runner".
  $alive = @(Get-RunnerProcesses)
  if ($alive.Count -gt 0) {
    Write-Log "OK: runner alive (PID $(($alive | ForEach-Object ProcessId) -join ', ')); left as is."
  } else {
    $orphans = @(Get-OrphanAgents)
    if ($orphans.Count -gt 0) {
      Write-Log ("WAIT: no runner, but cursor-agent PID $(($orphans | ForEach-Object ProcessId) -join ', ') is still " +
        "working on a task that is still in the inbox; not starting a runner that would run it twice.")
    } else {
      $p = Start-Runner
      Start-Sleep -Seconds $startVerifySeconds
      if (@(Get-RunnerProcesses | Where-Object { $_.ProcessId -eq $p.Id }).Count -eq 1) {
        Write-Log "STARTED: runner was missing; started PID $($p.Id)."
      } else {
        Write-Log "FAIL: runner was missing; started PID $($p.Id) but it is not running $startVerifySeconds s later."
        $code = 1
      }
    }
  }
} catch {
  Write-Log "FAIL: $_"
  $code = 1
} finally {
  $mutex.ReleaseMutex()
  $mutex.Dispose()
}
exit $code
