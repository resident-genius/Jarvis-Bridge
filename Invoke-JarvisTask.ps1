<#
.SYNOPSIS
  Stage 1 of the Jarvis <-> Cursor bridge: watches an inbox folder for Jarvis
  task packages (*.jarvis-task.json) and runs each one through Cursor's
  headless agent (cursor-agent -p).
.DESCRIPTION
  Supervised by default: every task is shown to {{OPERATOR_NAME}} for confirmation before
  it runs, and the workspace must be on the allowlist (allowed-workspaces.txt
  in the inbox folder). Use -AutoApprove to skip the per-task prompt (the
  allowlist is still enforced).
  Results land next to the inbox as <task-id>.jarvis-result.json; the package
  is moved to done\. {{OPERATOR_NAME}} pastes the JARVIS REPORT section back to Jarvis.
  Executor pool: up to -MaxParallel (default 3) tasks run at once, one
  cursor-agent per slot, oldest waiting task first into the lowest free slot.
  Slots are filled only when tasks wait. Two tasks with the same workspace
  never run together. Slot state: <inbox>\logs\executor-slots.json (shown in
  the bridge window); runner log: <inbox>\logs\runner.log.
  Every package must carry a positive timeout_minutes (no default; refused
  with a "failed" result otherwise). Workers heartbeat into
  <inbox>\logs\heartbeats; a heartbeat older than -HeartbeatStaleSeconds
  fails the task, stops its worker and frees the slot.
  A task whose cursor-agent dies on a Cursor backend error before producing
  any report (RetriableError / [resource_exhausted]) did no work, so it is
  re-queued up to -MaxInfraRetries times, -InfraRetryDelaySeconds apart,
  instead of being reported failed. A task that fails on its own merits is
  never re-run. Attempts used show up as infra_retries in the result.
.EXAMPLE
  .\Invoke-JarvisTask.ps1              # watch forever, confirm each task
  .\Invoke-JarvisTask.ps1 -Once        # process whatever is waiting, then exit
  .\Invoke-JarvisTask.ps1 -AutoApprove # no per-task prompt (allowlist still applies)
#>
param(
  [string]$Inbox = (Join-Path $env:USERPROFILE 'cursor-inbox'),
  [string]$AllowFile = '',
  [int]$PollSeconds = 5,
  [int]$TimeoutMinutes = 30,
  [ValidateRange(1, 8)][int]$MaxParallel = 3,
  [string]$TaskFilter = '*',
  [ValidateRange(5, 600)][int]$HeartbeatSeconds = 30,
  [ValidateRange(30, 86400)][int]$HeartbeatStaleSeconds = 300,
  [ValidateRange(0, 10)][int]$MaxInfraRetries = 2,
  [ValidateRange(5, 3600)][int]$InfraRetryDelaySeconds = 120,
  [switch]$AutoApprove,
  [switch]$Once
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
    @('PollSeconds', 'timeouts.runner_poll_seconds'),
    @('TimeoutMinutes', 'timeouts.runner_legacy_timeout_minutes'),
    @('MaxParallel', 'executors.cursor.max_parallel'),
    @('TaskFilter', 'executors.cursor.task_filter'),
    @('HeartbeatSeconds', 'timeouts.runner_heartbeat_seconds'),
    @('HeartbeatStaleSeconds', 'timeouts.runner_heartbeat_stale_seconds'),
    @('MaxInfraRetries', 'executors.cursor.max_infra_retries'),
    @('InfraRetryDelaySeconds', 'timeouts.runner_infra_retry_delay_seconds'))) {
  if ($PSBoundParameters.ContainsKey($bsParam[0])) { continue }
  # Out-of-range config values fail the parameter's validation and keep the default.
  try { Set-Variable -Name $bsParam[0] -Value (Get-BridgeSetting $bsParam[1] (Get-Variable -Name $bsParam[0] -ValueOnly)) } catch { }
}
$script:CursorCli = Get-BridgeSetting 'executors.cursor.cli' 'cursor-agent'
$script:CursorInstallUrl = Get-BridgeSetting 'executors.cursor.install_help_url' 'https://cursor.com/docs/cli'
$script:CursorDefaultMode = Get-BridgeSetting 'executors.cursor.default_mode' 'agent'
$script:CursorBaseArgs = [string[]](Get-BridgeSetting 'executors.cursor.base_args' @('-p', '--trust', '--output-format', 'json'))
$script:InfraRetryRegex = Get-BridgeSetting 'executors.cursor.infra_retry_stderr_regex' '(?i)RetriableError|\[resource_exhausted\]|\[unavailable\]|\[deadline_exceeded\]'
$script:TaskFileSuffix = Get-BridgeSetting 'executors.task_file_suffix' '.jarvis-task.json'
$script:ResultFileSuffix = Get-BridgeSetting 'executors.result_file_suffix' '.jarvis-result.json'
$script:RequiredTaskFields = [string[]](Get-BridgeSetting 'executors.required_task_fields' @('task_id', 'workspace', 'goal', 'acceptance_criteria'))
$script:OperatorName = Get-BridgeSetting 'executors.operator_name' '{{OPERATOR_NAME}}'
$script:AssistantName = Get-BridgeSetting 'executors.assistant_name' 'Jarvis'
$script:ReportHeading = Get-BridgeSetting 'executors.report_heading' '## JARVIS REPORT'
$script:ReportTitle = $script:ReportHeading.TrimStart('#', ' ')
$script:AttachmentsManifestName = Get-BridgeSetting 'paths.attachments_manifest_name' 'ATTACHMENTS.txt'
$script:MaxAttachments = Get-BridgeSetting 'limits.max_attachments' 10
$script:MaxAttachmentBytes = [long](Get-BridgeSetting 'limits.max_attachment_bytes' 10485760)
$script:MaxAttachmentMB = [long]($script:MaxAttachmentBytes / 1MB)
$script:TaskTimeoutMaxMinutes = Get-BridgeSetting 'timeouts.task_timeout_max_minutes' 720
$script:RunnerLogMaxBytes = [long](Get-BridgeSetting 'limits.runner_log_max_bytes' 2097152)
$script:RunnerLogKeepLines = Get-BridgeSetting 'limits.runner_log_keep_lines' 5000
$script:TimeoutLogTailLines = Get-BridgeSetting 'limits.timeout_log_tail_lines' 50
$script:TimeoutLogTailChars = Get-BridgeSetting 'limits.timeout_log_tail_chars' 8000
$script:BusyPollSeconds = Get-BridgeSetting 'timeouts.runner_busy_poll_seconds' 1
$script:HeartbeatGraceSeconds = Get-BridgeSetting 'timeouts.runner_heartbeat_startup_grace_seconds' 120
$script:ProcessTreeSkewSeconds = Get-BridgeSetting 'timeouts.runner_process_tree_skew_seconds' 5
$script:TaskRunnerMutexPrefix = Get-BridgeSetting 'scheduled_tasks.mutex_prefixes.task_runner' 'Local\JarvisTaskRunner-'
$bsSubdirs = [string[]](Get-BridgeSetting 'paths.inbox_subdirs' @('done', 'logs'))
$script:DoneDirName = if ($bsSubdirs.Count -ge 2) { $bsSubdirs[0] } else { 'done' }
$script:LogsDirName = if ($bsSubdirs.Count -ge 2) { $bsSubdirs[1] } else { 'logs' }
$script:AllowFileName = Get-BridgeSettingLeaf 'paths.allowed_workspaces_file' 'allowed-workspaces.txt'
$script:RunnerLogRel = Get-BridgeInboxRelative 'paths.runner_log' 'logs\runner.log'
$script:SlotStateRel = Get-BridgeInboxRelative 'paths.executor_slots_file' 'logs\executor-slots.json'
$script:HeartbeatsRel = Get-BridgeInboxRelative 'paths.heartbeats_dir' 'logs\heartbeats'
$script:ConhostExe = Get-BridgeSettingLeaf 'paths.conhost' 'conhost.exe'
# Every task package must carry a positive timeout_minutes (no default); the
# -TimeoutMinutes parameter is kept only so old command lines still parse.
# Heartbeats: each worker job writes <inbox>\logs\heartbeats\<task>.heartbeat.json
# every -HeartbeatSeconds (outside the task workspace). The pool fails a task
# whose heartbeat is older than -HeartbeatStaleSeconds, emails the failure
# with its log tail and frees the slot; the hard per-task timeout stays.
$script:HeartbeatSource = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class JarvisWorkerHeartbeat {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  struct PE32 {
    public uint dwSize, cntUsage, th32ProcessID; public IntPtr th32DefaultHeapID;
    public uint th32ModuleID, cntThreads, th32ParentProcessID; public int pcPriClassBase; public uint dwFlags;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string szExeFile;
  }
  [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);
  [DllImport("kernel32.dll", EntryPoint = "Process32FirstW", CharSet = CharSet.Unicode)] static extern bool First(IntPtr h, ref PE32 e);
  [DllImport("kernel32.dll", EntryPoint = "Process32NextW", CharSet = CharSet.Unicode)] static extern bool Next(IntPtr h, ref PE32 e);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  static Timer timer;
  static string path, taskId;
  static int self, graceSeconds;
  static DateTime started, selfStart;
  static long beats;
  // Beats while this worker has a live child (the agent) or is inside the
  // start-up grace; an agent that is gone while the worker is stuck, or a
  // frozen worker, stops the beat and the runner fails the task.
  public static void Start(string hbPath, string task, int periodSeconds, int grace) {
    path = hbPath; taskId = task; graceSeconds = grace;
    Process me = Process.GetCurrentProcess();
    self = me.Id; selfStart = me.StartTime; started = DateTime.Now;
    Tick(null);
    timer = new Timer(Tick, null, periodSeconds * 1000, periodSeconds * 1000);
  }
  static string Children() {
    IntPtr snap = CreateToolhelp32Snapshot(2, 0);
    if (snap == IntPtr.Zero || snap == new IntPtr(-1)) return null;
    StringBuilder sb = new StringBuilder();
    try {
      PE32 e = new PE32(); e.dwSize = (uint)Marshal.SizeOf(typeof(PE32));
      if (First(snap, ref e)) {
        do {
          if (e.th32ParentProcessID != (uint)self || e.th32ProcessID == (uint)self) continue;
          if (string.Equals(e.szExeFile, "conhost.exe", StringComparison.OrdinalIgnoreCase)) continue;
          try { if (Process.GetProcessById((int)e.th32ProcessID).StartTime < selfStart) continue; } catch { continue; }
          if (sb.Length > 0) sb.Append(',');
          sb.Append(e.szExeFile).Append(':').Append(e.th32ProcessID);
        } while (Next(snap, ref e));
      }
    } finally { CloseHandle(snap); }
    return sb.ToString();
  }
  static string Q(string s) { return "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\""; }
  static void Tick(object state) {
    try {
      string kids = Children();
      bool inGrace = (DateTime.Now - started).TotalSeconds < graceSeconds;
      if (kids != null && kids.Length == 0 && !inGrace) return;
      beats++;
      File.WriteAllText(path, "{\"task_id\":" + Q(taskId) + ",\"worker_pid\":" + self +
        ",\"mode\":\"timer\",\"beats\":" + beats + ",\"agent\":" + Q(kids ?? "unknown") +
        ",\"updated\":" + Q(DateTime.Now.ToString("o")) + "}");
    } catch { }
  }
}
'@
$script:HeartbeatSource = $script:HeartbeatSource.Replace('"conhost.exe"', '"' + $script:ConhostExe + '"')
# Executor pool: up to -MaxParallel cursor-agent jobs at once, one per slot.
# A slot is only filled when a task is waiting, so 1 queued task = 1 agent.
# -TaskFilter (task id wildcard) lets a throwaway runner share an inbox in tests.
$script:RunnerLog = $null
function Write-RunnerLog($level, $msg) {
  if (-not $script:RunnerLog) { return }
  $line = '{0} [pid {1}] {2} {3}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $PID, $level, $msg
  try {
    $fi = New-Object IO.FileInfo $script:RunnerLog
    if ($fi.Exists -and $fi.Length -gt $script:RunnerLogMaxBytes) {
      $keep = @(Get-Content -LiteralPath $script:RunnerLog -Tail $script:RunnerLogKeepLines)
      Set-Content -LiteralPath $script:RunnerLog -Value $keep -Encoding UTF8
    }
    [IO.File]::AppendAllText($script:RunnerLog, $line + "`r`n", [Text.Encoding]::UTF8)
  } catch { }
}
function Write-Info($msg) { Write-Host "[jarvis] $msg" -ForegroundColor Cyan; Write-RunnerLog 'INFO' $msg }
function Write-Warn($msg) { Write-Host "[jarvis] $msg" -ForegroundColor Yellow; Write-RunnerLog 'WARN' $msg }
function Write-Err($msg)  { Write-Host "[jarvis] $msg" -ForegroundColor Red; Write-RunnerLog 'ERROR' $msg }
# ---------- setup ----------
$doneDir = Join-Path $Inbox $script:DoneDirName
$logDir  = Join-Path $Inbox $script:LogsDirName
$allowFile = if ($AllowFile) { $AllowFile } else { Join-Path $Inbox $script:AllowFileName }
foreach ($d in @($Inbox, $doneDir, $logDir, (Split-Path -Parent $allowFile))) {
  if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d | Out-Null }
}
if (-not (Test-Path $allowFile)) {
  "# One allowed workspace per line. Tasks targeting anything else are refused.`r`n" |
    Set-Content $allowFile
  Write-Warn "Created $allowFile - add your project folders (one per line) before running tasks."
}
$agent = Get-Command $script:CursorCli -ErrorAction SilentlyContinue
if (-not $agent) {
  Write-Err @"
$($script:CursorCli) was not found on PATH.
Install it from inside Cursor: Ctrl+Shift+P -> "Install $($script:CursorCli)"
(or see $($script:CursorInstallUrl)), then re-run this script.
"@
  exit 1
}
$script:RunnerLog = Join-Path $Inbox $script:RunnerLogRel
$slotStateFile = Join-Path $Inbox $script:SlotStateRel
# One pool per inbox: a second runner would start every task twice. Exit the
# whole process (not just the script) so a -NoExit shell does not linger and
# look like a live runner to the watchdog.
$mutexName = $script:TaskRunnerMutexPrefix + ($Inbox.TrimEnd('\').ToLower() -replace '[^a-z0-9]', '_')
$script:RunnerMutex = [System.Threading.Mutex]::new($false, $mutexName)
$haveMutex = $false
try { $haveMutex = $script:RunnerMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $haveMutex = $true }
if (-not $haveMutex) {
  Write-Err "Another runner already owns $Inbox ($mutexName); exiting."
  [Environment]::Exit(3)
}
Write-Info "Using cursor-agent at $($agent.Source)"
$hbDir = Join-Path $Inbox $script:HeartbeatsRel
if (-not (Test-Path $hbDir)) { New-Item -ItemType Directory -Path $hbDir | Out-Null }
$script:HeartbeatFailures = 0
if ($script:BridgeSettingsError) { Write-Warn "bridge.config.json not used ($($script:BridgeSettingsError)); built-in values" }
elseif ($script:BridgeSettingsPath) { Write-Info "Settings: $($script:BridgeSettingsPath)" }
Write-Info "Watching $Inbox (poll every ${PollSeconds}s, timeout_minutes REQUIRED per task, up to $($script:TaskTimeoutMaxMinutes)m; no default)"
Write-Info "Worker heartbeats every ${HeartbeatSeconds}s in $hbDir; a task stale over ${HeartbeatStaleSeconds}s is failed and its slot freed"
Write-Info "Executor pool: up to $MaxParallel concurrent cursor-agent slot(s); task filter '$TaskFilter'"
function Get-AllowedWorkspaces {
  Get-Content $allowFile -ErrorAction SilentlyContinue |
    Where-Object { $_ -and -not $_.Trim().StartsWith('#') } |
    ForEach-Object { $_.Trim().TrimEnd('\') }
}
function Test-WorkspaceAllowed($workspace) {
  $norm = $workspace.Trim().TrimEnd('\')
  foreach ($a in Get-AllowedWorkspaces) {
    if ($norm -ieq $a) { return $true }
    # allow subdirectories of an allowed root
    if ($norm.ToLower().StartsWith($a.ToLower() + '\')) { return $true }
  }
  return $false
}
function Build-Prompt($task, $packagePath) {
  $lines = @()
  $lines += "You are working as $($script:OperatorName)'s coding agent on a task from $($script:AssistantName) (his assistant)."
  $lines += ''
  $lines += '## Goal'
  $lines += $task.goal
  $lines += ''
  if ($task.context_files -and $task.context_files.Count -gt 0) {
    $lines += '## Relevant files (read these first)'
    foreach ($f in $task.context_files) { $lines += "- $f" }
    $lines += ''
  }
  if ($packagePath -and $task.dev_files -and @($task.dev_files.PSObject.Properties).Count -gt 0) {
    $lines += '## Task package'
    $lines += "Package file: $packagePath"
    $lines += "The complete task JSON, including any dev_files (embedded file contents as filename -> text content), is at $packagePath. Read dev_files from that file."
    $lines += ''
  }
  if ($task.do_not_touch -and $task.do_not_touch.Count -gt 0) {
    $lines += '## Do not touch'
    foreach ($f in $task.do_not_touch) { $lines += "- $f" }
    $lines += ''
  }
  $lines += '## Acceptance criteria'
  $i = 1
  foreach ($c in $task.acceptance_criteria) { $lines += "$i. $c"; $i++ }
  $lines += ''
  $lines += '## Rules'
  $lines += "- Stay inside the workspace: $($task.workspace)"
  $lines += '- Preserve existing guards and behavior not in scope.'
  if ($task.allow_shell -eq $false) { $lines += '- Do not run shell commands; propose the changes instead.' }
  $lines += '- If an acceptance criterion cannot be met, say so explicitly rather than faking it.'
  $lines += "- If you produced files $($script:OperatorName) should see (screenshots, documents), list their workspace-relative paths, one per line, in $($script:AttachmentsManifestName) at the workspace root (max $($script:MaxAttachments) files, $($script:MaxAttachmentMB) MB total)."
  $lines += ''
  $lines += '## Report back'
  $lines += "End your final message with a section titled exactly `"$($script:ReportHeading)`" containing:"
  $lines += '1. Files changed (paths, one per line)'
  $lines += '2. Tests/commands run and their results'
  $lines += '3. Each acceptance criterion: PASS or FAIL with one line of evidence'
  $lines += "4. Open questions for $($script:OperatorName)/$($script:AssistantName) (or `"none`")"
  if ($task.report_back -and $task.report_back.Count -gt 0) {
    $lines += ''
    $lines += 'Also include:'
    foreach ($r in $task.report_back) { $lines += "- $r" }
  }
  return ($lines -join "`r`n")
}
function Move-AttachmentManifest($workspace, $destName) {
  $manifest = Join-Path $workspace $script:AttachmentsManifestName
  if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { return $null }
  $dest = Join-Path $logDir $destName
  Move-Item -LiteralPath $manifest -Destination $dest -Force
  return $dest
}
function Get-TaskAttachments($workspace, $taskId, $stamp) {
  # Entries are returned workspace-relative: bridge_core.collect_attachments
  # resolves them against the result's workspace and re-checks them before sending.
  $attached = New-Object System.Collections.ArrayList
  $skipped  = New-Object System.Collections.ArrayList
  $manifest = Join-Path $workspace $script:AttachmentsManifestName
  if (Test-Path -LiteralPath $manifest -PathType Leaf) {
    try {
      $root = [IO.Path]::GetFullPath($workspace.Trim()).TrimEnd('\')
      $manifestFull = [IO.Path]::GetFullPath($manifest)
      $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
      $total = [long]0
      foreach ($line in @(Get-Content -LiteralPath $manifest -TotalCount 100 -Encoding UTF8)) {
        $entry = ([string]$line).Trim([char[]]@([char]0xFEFF, [char]' ', [char]"`t"))
        if (-not $entry) { continue }
        $reason = $null; $full = $null; $size = [long]0
        try {
          $full = [IO.Path]::GetFullPath([IO.Path]::Combine($root, $entry))
          if (-not (($full -ieq $root) -or $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase))) { $reason = 'outside the task workspace' }
          elseif ($full -ieq $manifestFull) { $reason = "$($script:AttachmentsManifestName) itself is never attached" }
          elseif ([IO.Directory]::Exists($full)) { $reason = 'not a regular file' }
          elseif (-not [IO.File]::Exists($full)) { $reason = 'file not found' }
          elseif ($attached.Count -ge $script:MaxAttachments) { $reason = "more than $($script:MaxAttachments) files" }
          else {
            $size = ([IO.FileInfo]$full).Length
            if ($total + $size -gt $script:MaxAttachmentBytes) { $reason = "over $($script:MaxAttachmentMB) MB total" }
            elseif ($names.Contains([IO.Path]::GetFileName($full))) { $reason = 'duplicate file name' }
          }
        } catch { $reason = "invalid or unreadable path: $($_.Exception.Message)" }
        if ($reason) {
          $null = $skipped.Add([ordered]@{ path = $entry; reason = $reason })
        } else {
          $null = $names.Add([IO.Path]::GetFileName($full))
          $total += $size
          $null = $attached.Add($full.Substring($root.Length).TrimStart('\'))
        }
      }
    } catch {
      $null = $skipped.Add([ordered]@{ path = $script:AttachmentsManifestName; reason = "could not read manifest: $($_.Exception.Message)" })
    } finally {
      try {
        $archived = Move-AttachmentManifest $workspace "$taskId-$stamp.$($script:AttachmentsManifestName)"
        Write-Info "$($script:AttachmentsManifestName) archived: $archived"
      } catch { Write-Warn "Could not archive ${manifest}: $_" }
    }
    foreach ($s in $skipped) { Write-Warn "Attachment skipped: $($s.path) ($($s.reason))" }
  }
  return @{ attachments = @($attached); attachments_skipped = @($skipped) }
}
function Get-AgentFinalText($stdout) {
  # cursor-agent --output-format json wraps the agent's answer in a JSON
  # envelope. Reporting on the raw text leaked escaped newlines and the
  # trailing "session_id"/"usage" fields into the emailed JARVIS REPORT.
  if (-not $stdout) { return '' }
  foreach ($line in @($stdout -split "`r?`n")) {
    $trimmed = ([string]$line).Trim()
    if (-not $trimmed.StartsWith('{')) { continue }
    try { $obj = $trimmed | ConvertFrom-Json } catch { continue }
    if ($obj.PSObject.Properties['result'] -and $obj.result -is [string]) { return [string]$obj.result }
  }
  return [string]$stdout
}
function Get-TimeoutLogTail($outFile, $errFile, [int]$maxLines = $script:TimeoutLogTailLines, [int]$maxChars = $script:TimeoutLogTailChars) {
  # cursor-agent --output-format json can emit one huge line, so cap characters as well as lines.
  $parts = @(); $empty = $true
  foreach ($pair in @(@('stdout', $outFile), @('stderr', $errFile))) {
    $lines = @()
    try {
      if (Test-Path -LiteralPath $pair[1] -PathType Leaf) { $lines = @(Get-Content -LiteralPath $pair[1] -Tail $maxLines -ErrorAction Stop) }
    } catch { $lines = @("(could not read log: $($_.Exception.Message))") }
    $lines = @($lines | Where-Object { $null -ne $_ -and ([string]$_).Trim() })
    if ($lines.Count -gt 0) { $empty = $false } else { $lines = @('(empty)') }
    $parts += "--- last $maxLines lines of $($pair[0]) ($($pair[1])) ---"
    $parts += $lines
  }
  $text = ($parts -join "`n"); $truncated = $false
  if ($text.Length -gt $maxChars) {
    $truncated = $true
    $text = "...[log excerpt truncated to the last $maxChars characters]`n" + $text.Substring($text.Length - $maxChars)
  }
  return @{ text = $text; truncated = $truncated; empty = $empty }
}
function Get-TimeoutReport($taskId, $started, $finished, $limitMinutes, $tail, $outFile, $errFile, $partialReport) {
  $mins = [int][Math]::Round(($finished - $started).TotalMinutes)
  $ran = if ($mins -lt 1) { 'less than a minute' } elseif ($mins -eq 1) { 'about 1 minute' } else { "about $mins minutes" }
  $lines = @("Task $taskId timed out. The runner stopped it after $ran because it hit the $limitMinutes-minute time limit.")
  if ($tail.empty) { $lines += "It produced no output at all before it was stopped, so there is no result and no $($script:ReportTitle)." }
  else { $lines += "It did not finish, so no result or $($script:ReportTitle) was produced. The last lines of its log are in the stderr field." }
  if ($tail.truncated) { $lines += 'The log excerpt was shortened to keep this email small.' }
  $lines += "Full logs on the runner machine: $outFile and $errFile"
  if ($partialReport) { $lines += ''; $lines += 'Partial report found before the timeout:'; $lines += $partialReport }
  return ($lines -join "`n")
}
function Get-TaskTimeoutMinutes($task) {
  # Required per-task timeout_minutes (> 0, rounded up, 720 = 12h ceiling).
  # $null when it is missing or not a positive number: the task is refused.
  $maxMinutes = $script:TaskTimeoutMaxMinutes
  if (-not $task -or -not $task.PSObject.Properties['timeout_minutes'] -or $null -eq $task.timeout_minutes) { return $null }
  if ($task.timeout_minutes -is [bool]) { return $null }
  $parsed = 0.0
  if (-not [double]::TryParse([string]$task.timeout_minutes, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) { return $null }
  if ([double]::IsNaN($parsed) -or [double]::IsInfinity($parsed) -or $parsed -le 0) { return $null }
  if ($parsed -gt $maxMinutes) { return [int]$maxMinutes }
  return [int][Math]::Ceiling($parsed)
}
function Write-Result($taskId, $result) {
  # .tmp + rename: the bridge only picks up *.jarvis-result.json, never a partial file
  $path = Join-Path $Inbox "$taskId$($script:ResultFileSuffix)"
  $tmp = "$path.tmp"
  $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $tmp -Encoding UTF8
  Move-Item -LiteralPath $tmp -Destination $path -Force
  Write-Info "Result written: $path"
}
function Write-SkipResult($taskId, $reason, $task, $status = 'skipped') {
  # Early returns used to move the package to done\ with no result file, so
  # Loop B never emailed and cursor-agent never started. Always report.
  $ws = if ($task -and $task.workspace) { [string]$task.workspace } else { '' }
  $criteria = @()
  if ($task -and $task.acceptance_criteria) { $criteria = @($task.acceptance_criteria) }
  $now = (Get-Date).ToString('o')
  Write-Result $taskId @{
    task_id = [string]$taskId
    status = [string]$status
    exit_code = 1
    workspace = $ws
    acceptance_criteria = $criteria
    jarvis_report = "The Cursor runner did not start cursor-agent. $reason"
    stderr = [string]$reason
    stdout = ''
    timed_out = $false
    attachments = @()
    started = $now
    finished = $now
  }
}
function Start-TaskRun($packagePath, [int]$slot) {
  # Validates and launches one task in a background job. Returns the run, or
  # $null when the task was skipped (a skip result has been written).
  $taskId = [IO.Path]::GetFileNameWithoutExtension($packagePath) -replace ([regex]::Escape([IO.Path]::GetFileNameWithoutExtension($script:TaskFileSuffix)) + '$'), ''
  Write-Info "--- Task: $taskId (slot $slot) ---"
  try {
    $task = Get-Content $packagePath -Raw | ConvertFrom-Json
  } catch {
    $reason = "Could not parse ${packagePath}: $_"
    Write-Err $reason
    Write-SkipResult $taskId $reason $null
    return
  }
  foreach ($field in $script:RequiredTaskFields) {
    if (-not $task.$field) {
      $reason = "Package is missing required field '$field'."
      Write-Err "$reason Skipping."
      Write-SkipResult $taskId $reason $task
      return
    }
  }
  if ($task.task_id -ne $taskId) {
    $reason = "task_id '$($task.task_id)' does not match file name '$taskId'."
    Write-Err "$reason Skipping."
    Write-SkipResult $taskId $reason $task
    return
  }
  $jobTimeoutMinutes = Get-TaskTimeoutMinutes $task
  if ($null -eq $jobTimeoutMinutes) {
    $got = if ($task.PSObject.Properties['timeout_minutes']) { "'$($task.timeout_minutes)'" } else { 'nothing (field missing)' }
    $reason = "FAILED: timeout_minutes is required and must be a positive number of minutes (max $($script:TaskTimeoutMaxMinutes)); got $got. The task was NOT run and no worker was started; there is no default timeout."
    Write-Err $reason
    Write-SkipResult $taskId $reason $task 'failed'
    return
  }
  if (-not (Test-Path -LiteralPath $task.workspace)) {
    if (Test-WorkspaceAllowed $task.workspace) {
      New-Item -ItemType Directory -Force -Path $task.workspace | Out-Null
      Write-Info "Created missing workspace: $($task.workspace)"
    } else {
      $reason = "Workspace does not exist and is not on the allowlist: $($task.workspace)"
      Write-Err $reason
      Write-SkipResult $taskId $reason $task
      return
    }
  }
  if (-not (Test-WorkspaceAllowed $task.workspace)) {
    if ($AutoApprove) {
      $reason = "Workspace not on allowlist ($allowFile). Refusing to run under -AutoApprove."
      Write-Err $reason
      Write-SkipResult $taskId $reason $task
      return
    }
    Write-Warn "Workspace is NOT on the allowlist:"
    Write-Warn "  $($task.workspace)"
    $add = Read-Host 'Add it to the allowlist and continue? [y/N]'
    if ($add -notmatch '^[Yy]') {
      Write-Warn 'Skipped.'
      Write-SkipResult $taskId "$($script:OperatorName) declined to add the workspace to the allowlist: $($task.workspace)" $task
      return
    }
    Add-Content $allowFile $task.workspace.Trim()
    Write-Info 'Allowlist updated.'
  }
  $mode = if ($task.mode) { $task.mode } else { $script:CursorDefaultMode }
  $prompt = Build-Prompt $task $packagePath
  # --workspace goes before --output-format, as it always has.
  $args = @()
  $wsAdded = $false
  foreach ($a in $script:CursorBaseArgs) {
    if (-not $wsAdded -and $a -eq '--output-format') { $args += @('--workspace', $task.workspace); $wsAdded = $true }
    $args += $a
  }
  if (-not $wsAdded) { $args += @('--workspace', $task.workspace) }
  if ($mode -eq 'agent') { $args += '--force' }
  elseif ($mode -eq 'plan') { $args += @('--mode', 'plan') }
  elseif ($mode -eq 'ask') { $args += @('--mode', 'ask') }
  if ($task.model) { $args += @('--model', $task.model) }
  $args += $prompt
  Write-Info "Mode: $mode | Workspace: $($task.workspace)"
  Write-Host ''
  Write-Host 'Goal:' $task.goal -ForegroundColor White
  Write-Host ''
  if (-not $AutoApprove) {
    $go = Read-Host 'Run this task in Cursor? [Y/n]'
    if ($go -match '^[Nn]') { Write-Warn "Skipped by $($script:OperatorName)."; return }
  }
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $outFile = Join-Path $logDir "$taskId-$stamp.stdout.log"
  $errFile = Join-Path $logDir "$taskId-$stamp.stderr.log"
  $started = Get-Date
  try {
    $stale = Move-AttachmentManifest $task.workspace "$taskId-$stamp.stale.$($script:AttachmentsManifestName)"
    if ($stale) { Write-Warn "Archived a stale $($script:AttachmentsManifestName) left before this run: $stale" }
  } catch { Write-Warn "Could not archive stale $($script:AttachmentsManifestName): $_" }
  Write-Info "Launching $($script:CursorCli) (timeout ${jobTimeoutMinutes}m)..."
  $agentCmd = (Get-Command $script:CursorCli -ErrorAction Stop).Source
  $hbFile = Join-Path $hbDir "$taskId.heartbeat.json"
  Remove-Item -LiteralPath $hbFile -Force -ErrorAction SilentlyContinue
  $job = Start-Job -Name "jarvis-$taskId" -ScriptBlock {
    param($cmdPath, $argList, $outPath, $errPath, $hbPath, $hbTaskId, $hbSeconds, $hbSource, $hbGrace)
    try {
      Add-Type -TypeDefinition $hbSource -Language CSharp -ErrorAction Stop
      [JarvisWorkerHeartbeat]::Start($hbPath, $hbTaskId, $hbSeconds, $hbGrace)
    } catch {
      # No heartbeat possible: mark the run unsupervised (hard timeout only).
      try { Set-Content -LiteralPath $hbPath -Encoding UTF8 -Value (@{ task_id = $hbTaskId; worker_pid = $PID; mode = 'none'; error = "$_" } | ConvertTo-Json -Compress) } catch { }
    }
    & $cmdPath @argList > $outPath 2> $errPath
    $LASTEXITCODE
  } -ArgumentList @($agentCmd, $args, $outFile, $errFile, $hbFile, $taskId, $HeartbeatSeconds, $script:HeartbeatSource, [int]$script:HeartbeatGraceSeconds)
  Write-Info "Slot ${slot}: STARTED $taskId (job $($job.Id), heartbeat $hbFile)"
  return [PSCustomObject]@{
    Slot = $slot; TaskId = $taskId; Task = $task; PackagePath = $packagePath; Mode = $mode
    Stamp = $stamp; OutFile = $outFile; ErrFile = $errFile; Started = $started
    TimeoutMinutes = $jobTimeoutMinutes; Deadline = $started.AddMinutes($jobTimeoutMinutes); Job = $job
    HeartbeatFile = $hbFile; HeartbeatUnix = $null; WorkerPid = 0; HeartbeatMode = ''; StaleSeconds = 0
    RetryRequested = $false
  }
}
function Update-RunHeartbeat($run) {
  # Returns the heartbeat age in seconds (age since start when the worker
  # never beat) and caches worker pid / mode / last beat on the run.
  $fi = New-Object IO.FileInfo $run.HeartbeatFile
  if (-not $fi.Exists) { return [int]((Get-Date) - $run.Started).TotalSeconds }
  $run.HeartbeatUnix = [DateTimeOffset]::new($fi.LastWriteTime).ToUnixTimeSeconds()
  if (-not $run.WorkerPid -or -not $run.HeartbeatMode) {
    try {
      $hb = [IO.File]::ReadAllText($fi.FullName) | ConvertFrom-Json
      if ($hb.worker_pid) { $run.WorkerPid = [int]$hb.worker_pid }
      if ($hb.mode) { $run.HeartbeatMode = [string]$hb.mode }
    } catch { }
  }
  return [int]((Get-Date) - $fi.LastWriteTime).TotalSeconds
}
function Stop-WorkerTree($run) {
  # Kills the worker job process and everything it started (it may be
  # frozen, so Stop-Job alone could hang).
  $root = [int]$run.WorkerPid
  if ($root -le 0) { Stop-Job $run.Job -ErrorAction SilentlyContinue; return 'worker pid unknown; Stop-Job used' }
  $killed = New-Object 'System.Collections.Generic.List[string]'
  $tree = New-Object 'System.Collections.Generic.HashSet[int]'
  $null = $tree.Add($root)
  try {
    $procs = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    do {
      $more = @($procs | Where-Object { $tree.Contains([int]$_.ParentProcessId) -and -not $tree.Contains([int]$_.ProcessId) -and $_.CreationDate -ge $run.Started.AddSeconds(-$script:ProcessTreeSkewSeconds) })
      foreach ($p in $more) { $null = $tree.Add([int]$p.ProcessId) }
    } while ($more.Count -gt 0)
    foreach ($p in @($procs | Where-Object { $tree.Contains([int]$_.ProcessId) } | Sort-Object { $_.ProcessId -eq $root })) {
      Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
      $killed.Add("$($p.Name):$($p.ProcessId)")
    }
  } catch { Stop-Process -Id $root -Force -ErrorAction SilentlyContinue; $killed.Add("worker:$root") }
  return ($killed -join ', ')
}
function Get-HeartbeatReport($run, $finished, $tail, $killed, $partialReport) {
  $mins = [int][Math]::Round(($finished - $run.Started).TotalMinutes)
  $ran = if ($mins -lt 1) { 'less than a minute' } elseif ($mins -eq 1) { 'about 1 minute' } else { "about $mins minutes" }
  $staleMin = [Math]::Round($run.StaleSeconds / 60.0, 1)
  $lines = @("Task $($run.TaskId) FAILED: its worker stopped sending heartbeats. The last heartbeat was $($run.StaleSeconds) s ($staleMin min) ago, over the $HeartbeatStaleSeconds-second limit, so the runner declared the worker dead after $ran, stopped it and freed executor slot $($run.Slot). The $($run.TimeoutMinutes)-minute hard timeout was not reached.")
  if ($tail.empty) { $lines += "The worker produced no output at all, so there is no result and no $($script:ReportTitle)." }
  else { $lines += "It did not finish, so no result or $($script:ReportTitle) was produced. The last lines of its log are in the stderr field." }
  if ($tail.truncated) { $lines += 'The log excerpt was shortened to keep this email small.' }
  $lines += "Stopped processes: $killed"
  $lines += "Full logs on $($env:COMPUTERNAME): $($run.OutFile) and $($run.ErrFile)"
  if ($partialReport) { $lines += ''; $lines += 'Partial report found before the worker died:'; $lines += $partialReport }
  return ($lines -join "`n")
}
function Complete-TaskRun($run) {
  # Called once the run's job has ended, passed its deadline, or its worker
  # heartbeat went stale; writes the result.
  $taskId = $run.TaskId; $task = $run.Task; $packagePath = $run.PackagePath; $mode = $run.Mode
  $stamp = $run.Stamp; $outFile = $run.OutFile; $errFile = $run.ErrFile; $started = $run.Started
  $jobTimeoutMinutes = $run.TimeoutMinutes; $job = $run.Job
  $timedOut = $false; $heartbeatStale = $false; $killed = ''; $agentExit = 0
  if ($run.StaleSeconds -gt 0 -and [string]$job.State -notin @('Completed', 'Failed', 'Stopped')) {
    $heartbeatStale = $true; $agentExit = -1
    Write-Err "Task $taskId FAILED: worker heartbeat stale for $($run.StaleSeconds)s (limit ${HeartbeatStaleSeconds}s); stopping worker PID $($run.WorkerPid)."
    $killed = Stop-WorkerTree $run
    Write-Err "Stopped: $killed"
  } else {
    try {
      if ([string]$job.State -in @('Completed', 'Failed', 'Stopped')) { $agentExit = [int](Receive-Job $job) }
      else { throw [System.TimeoutException]::new("t") }
    } catch {
      $timedOut = $true; Stop-Job $job -ErrorAction SilentlyContinue
      Write-Err "Task timed out after $jobTimeoutMinutes minutes."; $agentExit = -1
    }
  }
  Remove-Job $job -Force -ErrorAction SilentlyContinue
  try {
    if ($heartbeatStale) { Move-Item -LiteralPath $run.HeartbeatFile -Destination (Join-Path $hbDir "$taskId-$stamp.stale.heartbeat.json") -Force -ErrorAction Stop }
    else { Remove-Item -LiteralPath $run.HeartbeatFile -Force -ErrorAction SilentlyContinue }
  } catch { }
  Write-Info "Slot $($run.Slot): FINISHED $taskId (exit $agentExit, timed_out $timedOut)"
  $proc = [PSCustomObject]@{ ExitCode = $agentExit; Id = 0 }
  $finished = Get-Date
  $stdout = if (Test-Path $outFile) { Get-Content $outFile -Raw } else { '' }
  $stderr = if (Test-Path $errFile) { Get-Content $errFile -Raw } else { '' }
  if ($null -eq $stdout) { $stdout = '' }
  if ($null -eq $stderr) { $stderr = '' }
  $report = ''
  $m = [regex]::Match($stdout, [regex]::Escape($script:ReportHeading) + '([\s\S]*)')
  if ($m.Success) { $report = $m.Groups[1].Value.Trim() }
  if ($report.Length -gt 200000) { $report = $report.Substring(0, 200000) + "`n...[truncated]" }
  $maxRC = 200000
  if ($stdout.Length -gt $maxRC) { $om = $stdout.Length - $maxRC; $stdout = $stdout.Substring(0, $maxRC) + "`n...[truncated $om chars; full log: $outFile]" }
  if ($stderr.Length -gt $maxRC) { $om = $stderr.Length - $maxRC; $stderr = $stderr.Substring(0, $maxRC) + "`n...[truncated $om chars; full log: $errFile]" }
  if ($timedOut) {
    try {
      $tail = Get-TimeoutLogTail $outFile $errFile
      $stderr = $tail.text
      $report = Get-TimeoutReport $taskId $started $finished $jobTimeoutMinutes $tail $outFile $errFile $report
    } catch {
      $report = "Task $taskId timed out after the $jobTimeoutMinutes-minute limit and produced no result. (Could not read its logs: $($_.Exception.Message))"
    }
  }
  if ($heartbeatStale) {
    try {
      $tail = Get-TimeoutLogTail $outFile $errFile
      $stderr = $tail.text
      $report = Get-HeartbeatReport $run $finished $tail $killed $report
    } catch {
      $report = "Task $taskId FAILED: worker heartbeat stale for $($run.StaleSeconds) s; the runner stopped it and freed slot $($run.Slot). (Could not read its logs: $($_.Exception.Message))"
    }
  }
  $infraGaveUp = $false
  if (-not $timedOut -and -not $heartbeatStale -and $agentExit -ne 0 -and
      $MaxInfraRetries -gt 0 -and (Test-InfraFailure $stdout $stderr)) {
    $tries = [int]$script:InfraAttempts[$packagePath]
    if ($tries -lt $MaxInfraRetries) {
      $script:InfraAttempts[$packagePath] = $tries + 1
      $script:InfraRetryAfter[$packagePath] = (Get-Date).AddSeconds($InfraRetryDelaySeconds)
      $run.RetryRequested = $true
      Write-Warn "Task $taskId hit a Cursor backend error before producing a report (attempt $($tries + 1) of $($MaxInfraRetries + 1)); re-queuing it in ${InfraRetryDelaySeconds}s. Logs: $errFile"
      return
    }
    $infraGaveUp = $true
    Write-Err "Task $taskId hit a Cursor backend error $($MaxInfraRetries + 1) times; giving up and reporting it failed."
  }
  $infraRetried = [int]$script:InfraAttempts[$packagePath]
  $script:InfraAttempts.Remove($packagePath)
  $script:InfraRetryAfter.Remove($packagePath)
  $att = Get-TaskAttachments $task.workspace $taskId $stamp
  $result = @{
    task_id     = [string]$taskId
    started     = [string]$started.ToString('o')
    finished    = [string]$finished.ToString('o')
    exit_code   = [int]$proc.ExitCode
    timed_out   = [bool]$timedOut
    timeout_minutes = [int]$jobTimeoutMinutes
    workspace   = [string]$task.workspace
    mode        = [string]$mode
    stdout      = [string]$stdout
    stderr      = [string]$stderr
    jarvis_report = [string]$report
    attachments = @($att.attachments)
    attachments_skipped = @($att.attachments_skipped)
  }
  if ($infraRetried -gt 0) { $result.infra_retries = $infraRetried }
  if ($infraGaveUp) {
    $result.status = 'failed'
    $result.jarvis_report = "Task $taskId never reached the agent: cursor-agent lost its connection to the Cursor backend and the reconnect was refused ([resource_exhausted]). The runner retried $infraRetried time(s) and every attempt failed the same way, so no work was done and the workspace is untouched. This is a Cursor-side error, not a problem with the task - re-file it as-is once the backend recovers."
  }
  if ($heartbeatStale) {
    $result.status = 'failed'
    $result.heartbeat_stale = $true
    $result.heartbeat_age_seconds = [int]$run.StaleSeconds
    $result.heartbeat_limit_seconds = [int]$HeartbeatStaleSeconds
  }
  Write-Result $taskId $result
  Move-Item $packagePath (Join-Path $doneDir ([IO.Path]::GetFileName($packagePath))) -Force
  if ($report) {
    Write-Host ''
    Write-Host "==================== $($script:ReportTitle) ====================" -ForegroundColor Green
    Write-Host $report
    Write-Host '=======================================================' -ForegroundColor Green
  } else {
    Write-Warn "No $($script:ReportHeading) section found in the output - paste the result file back to $($script:AssistantName)."
  }
}
function Get-NormalizedPath($p) { if ($p) { ([string]$p).Trim().TrimEnd('\').ToLower() } else { '' } }
function Get-PackageWorkspace($path) {
  try { return Get-NormalizedPath ((Get-Content -LiteralPath $path -Raw | ConvertFrom-Json).workspace) } catch { return '' }
}
function Get-ForeignAgentWorkspaces {
  # Workspaces of live headless cursor-agents this pool did not start (e.g.
  # left by a runner that was replaced); a task there would run twice.
  $out = New-Object 'System.Collections.Generic.HashSet[string]'
  try {
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction Stop)) {
      $cl = [string]$p.CommandLine
      if ($cl -notmatch ('(?i)' + [regex]::Escape($script:CursorCli)) -or $cl -notmatch '\s-p\s') { continue }
      $m = [regex]::Match($cl, '--workspace\s+(?:"([^"]+)"|(\S+))')
      if ($m.Success) { $null = $out.Add((Get-NormalizedPath ($m.Groups[1].Value + $m.Groups[2].Value))) }
    }
  } catch { Write-Warn "Could not list cursor-agent processes: $_" }
  return ,$out
}
function Write-SlotState($slots, [int]$queued, [bool]$stopped = $false) {
  # Read by bridge_gui.py (bridge_core.read_executor_slots); .tmp + rename.
  $now = Get-Date
  $list = @()
  for ($i = 0; $i -lt $slots.Count; $i++) {
    $r = $slots[$i]
    if ($r) {
      $list += [ordered]@{ slot = $i + 1; state = 'running'; task_id = [string]$r.TaskId
        started = $r.Started.ToString('o'); started_unix = [DateTimeOffset]::new($r.Started).ToUnixTimeSeconds()
        workspace = [string]$r.Task.workspace; heartbeat_unix = $r.HeartbeatUnix; worker_pid = $r.WorkerPid
        heartbeat_mode = [string]$r.HeartbeatMode }
    } else { $list += [ordered]@{ slot = $i + 1; state = 'idle' } }
  }
  $state = [ordered]@{ runner_pid = $PID; max_slots = $slots.Count; running = @($slots | Where-Object { $_ }).Count
    queued = $queued; stopped = $stopped; task_filter = $TaskFilter
    heartbeat_seconds = $HeartbeatSeconds; heartbeat_stale_seconds = $HeartbeatStaleSeconds
    heartbeat_failures = $script:HeartbeatFailures
    updated = $now.ToString('o'); updated_unix = [DateTimeOffset]::new($now).ToUnixTimeSeconds(); slots = $list }
  $tmp = "$slotStateFile.$PID.tmp"
  try {
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $slotStateFile -Force
  } catch { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
}
function Move-PackageToDone($path) {
  if (Test-Path -LiteralPath $path) { Move-Item -LiteralPath $path (Join-Path $doneDir ([IO.Path]::GetFileName($path))) -Force }
}
# Infra retries: cursor-agent dies with "RetriableError: [resource_exhausted]"
# when all three of its own reconnects to the Cursor backend are refused. The
# agent never reaches the task, so the package used to go to done\ with an
# empty report and Jarvis had to re-file the whole job as -002. Retry here
# instead. Keyed by package path: attempts made, and the earliest next start.
$script:InfraAttempts = @{}
$script:InfraRetryAfter = @{}
function Test-InfraFailure($stdout, $stderr) {
  # Only an agent that produced no report at all; a task that ran and failed
  # on its own merits must never be silently re-run.
  if ($stdout -match [regex]::Escape($script:ReportHeading)) { return $false }
  return ($stderr -match $script:InfraRetryRegex)
}
# ---------- main loop ----------
$slots = New-Object object[] $MaxParallel
$blockedNoted = @{}
try {
  do {
    # 1. Reap slots whose job ended, ran past its timeout, or whose worker
    #    heartbeat went stale (dead/frozen worker: fail it, free the slot).
    for ($i = 0; $i -lt $MaxParallel; $i++) {
      $run = $slots[$i]
      if (-not $run) { continue }
      $ended = [string]$run.Job.State -in @('Completed', 'Failed', 'Stopped')
      if (-not $ended) {
        $hbAge = Update-RunHeartbeat $run
        if ($run.HeartbeatMode -ne 'none' -and $hbAge -gt $HeartbeatStaleSeconds -and (Get-Date) -lt $run.Deadline) {
          $run.StaleSeconds = $hbAge
          $script:HeartbeatFailures++
          Write-Warn "Slot $($i + 1): $($run.TaskId) heartbeat stale ${hbAge}s > ${HeartbeatStaleSeconds}s -- failing it and freeing the slot."
        }
      }
      if ($ended -or $run.StaleSeconds -gt 0 -or (Get-Date) -ge $run.Deadline) {
        try { Complete-TaskRun $run } catch { Write-Err "Unexpected error on $($run.TaskId): $_" }
        finally {
          # A retried package stays in the inbox so the fill pass picks it up again.
          if (-not $run.RetryRequested) { Move-PackageToDone $run.PackagePath }
          $slots[$i] = $null
        }
        Write-Info "Slot $($i + 1): idle"
      }
    }
    # 2. Fill free slots, oldest task first. Tasks sharing a workspace with a
    #    running task (or a foreign live cursor-agent) wait for it to finish.
    $startedCount = 0
    $packages = @(Get-ChildItem (Join-Path $Inbox ('*' + $script:TaskFileSuffix)) -ErrorAction SilentlyContinue |
      Where-Object { ($_.Name -replace ([regex]::Escape($script:TaskFileSuffix) + '$'), '') -like $TaskFilter } |
      Sort-Object LastWriteTime, Name)
    $runningPaths = @($slots | Where-Object { $_ } | ForEach-Object { $_.PackagePath })
    $waiting = @($packages | Where-Object { $runningPaths -notcontains $_.FullName })
    $foreign = $null
    foreach ($p in $waiting) {
      $free = @(0..($MaxParallel - 1) | Where-Object { -not $slots[$_] })
      if ($free.Count -eq 0) { break }
      $after = $script:InfraRetryAfter[$p.FullName]
      if ($after -and (Get-Date) -lt $after) {
        $why = "backing off until $($after.ToString('HH:mm:ss')) after a Cursor backend failure"
        if ($blockedNoted[$p.Name] -ne $why) { Write-Warn "Holding $($p.Name): $why."; $blockedNoted[$p.Name] = $why }
        continue
      }
      $ws = Get-PackageWorkspace $p.FullName
      if ($ws) {
        $busy = @($slots | Where-Object { $_ -and (Get-NormalizedPath $_.Task.workspace) -eq $ws })
        if ($null -eq $foreign) { $foreign = Get-ForeignAgentWorkspaces }
        $why = if ($busy.Count -gt 0) { "slot $($busy[0].Slot) is running $($busy[0].TaskId) in the same workspace" }
               elseif ($foreign.Contains($ws)) { 'a cursor-agent this runner did not start is still working in its workspace' }
        if ($why) {
          if ($blockedNoted[$p.Name] -ne $why) { Write-Warn "Holding $($p.Name): $why."; $blockedNoted[$p.Name] = $why }
          continue
        }
      }
      $blockedNoted.Remove($p.Name)
      $slot = $free[0]
      $run = $null
      try { $run = Start-TaskRun $p.FullName ($slot + 1) } catch { Write-Err "Unexpected error on $($p.Name): $_" }
      if ($run) { $slots[$slot] = $run; $startedCount++ }
      else { Move-PackageToDone $p.FullName }
    }
    $runningNow = @($slots | Where-Object { $_ }).Count
    $runningPaths = @($slots | Where-Object { $_ } | ForEach-Object { $_.PackagePath })
    $queued = @($packages | Where-Object { $runningPaths -notcontains $_.FullName -and (Test-Path -LiteralPath $_.FullName) }).Count
    Write-SlotState $slots $queued
    if ($Once -and $runningNow -eq 0 -and $startedCount -eq 0) { break }
    # Poll fast while agents run so finished slots are reaped and refilled promptly.
    if ($runningNow -gt 0) { Start-Sleep -Seconds $script:BusyPollSeconds } else { Start-Sleep -Seconds $PollSeconds }
  } while ($true)
} finally {
  Write-SlotState $slots 0 $true
  try { $script:RunnerMutex.ReleaseMutex() } catch { }
}
Write-Info 'Done (-Once).'
