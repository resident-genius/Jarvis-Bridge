<#
.SYNOPSIS
  Canonical (re)start of the Jarvis bridge. Stops every bridge process
  running from this folder with the default config, launches exactly one
  bridge_gui.py (detached, so it outlives this shell), and verifies that a
  single instance is left running.
.DESCRIPTION
  The Windows logon autostart (Scheduled Task "JarvisBridge Autostart",
  managed by Register-JarvisBridgeAutostart.ps1) runs this script with
  -IfNotRunning. bridge_gui.py takes a per-config lock, so a second copy
  exits at once; it starts polling on launch once the Gmail address and app
  password are set.
  The task runner (Invoke-JarvisTask.ps1) is a separate process and is
  never touched.
.EXAMPLE
  .\Start-JarvisBridge.ps1                 # restart, leave one instance
  .\Start-JarvisBridge.ps1 -StopOnly       # stop the bridge
  .\Start-JarvisBridge.ps1 -IfNotRunning   # start only if none is running
  .\Start-JarvisBridge.ps1 -Python D:\py\pythonw.exe
#>
param(
  [string]$Python = '',
  [int]$WaitSeconds = 20,
  [switch]$StopOnly,
  [switch]$IfNotRunning
)
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$gui = Join-Path $dir 'bridge_gui.py'
$bridgeSettingsScript = Join-Path $dir 'BridgeSettings.ps1'
try { if (Test-Path -LiteralPath $bridgeSettingsScript) { . $bridgeSettingsScript } } catch { }
if (-not (Get-Command Get-BridgeSetting -ErrorAction SilentlyContinue)) {
  function Get-BridgeSetting([string]$Key, $Default) { return ,$Default }
}
if (-not $PSBoundParameters.ContainsKey('WaitSeconds')) {
  try { $WaitSeconds = Get-BridgeSetting 'timeouts.start_bridge_stop_wait_seconds' $WaitSeconds } catch { }
}
$stopPollMs = Get-BridgeSetting 'timeouts.start_bridge_stop_poll_ms' 300
$verifySeconds = Get-BridgeSetting 'timeouts.start_bridge_verify_seconds' 4

function Get-BridgeProcesses {
  Get-CimInstance Win32_Process |
    Where-Object {
      $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -and
      $_.CommandLine.IndexOf($dir, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
      $_.CommandLine -match 'bridge_gui|gmail_bridge_cli' -and
      $_.CommandLine -notmatch '--config'
    }
}

function Resolve-Pythonw {
  if ($Python) { return $Python }
  $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $py = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($py) {
    $exe = (& $py.Source -3 -c 'import sys; print(sys.executable)').Trim()
    $w = Join-Path (Split-Path -Parent $exe) 'pythonw.exe'
    if (Test-Path $w) { return $w }
  }
  throw 'pythonw.exe not found. Install Python 3 or pass -Python <path to pythonw.exe>.'
}

$old = @(Get-BridgeProcesses)
if ($IfNotRunning -and -not $StopOnly -and $old.Count -eq 1) {
  Write-Host "OK: bridge already running (PID $($old[0].ProcessId)); left as is."
  exit 0
}
foreach ($p in $old) {
  Write-Host "Stopping bridge PID $($p.ProcessId): $($p.CommandLine)"
  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while (@(Get-BridgeProcesses).Count -gt 0 -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds $stopPollMs }
if (@(Get-BridgeProcesses).Count -gt 0) { throw 'Old bridge process(es) did not exit.' }
if ($StopOnly) { Write-Host 'Bridge stopped.'; exit 0 }

$pyw = Resolve-Pythonw
$cmdLine = '"{0}" "{1}"' -f $pyw, $gui
# Win32_Process.Create: the bridge is not a child of this shell, so it keeps
# running after the shell (or an agent session that ran it) ends.
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine = $cmdLine; CurrentDirectory = $dir }
if ($r.ReturnValue -ne 0) { throw "Launch failed (Win32_Process.Create returned $($r.ReturnValue))." }
$newPid = [int]$r.ProcessId
Write-Host "Launched bridge PID $newPid : $cmdLine"

Start-Sleep -Seconds $verifySeconds
$now = @(Get-BridgeProcesses)
if ($now.Count -ne 1 -or $now[0].ProcessId -ne $newPid) {
  $now | ForEach-Object { Write-Host "  running: PID $($_.ProcessId) $($_.CommandLine)" }
  throw "Expected exactly one bridge (PID $newPid); found $($now.Count)."
}
Write-Host "OK: exactly one bridge instance running (PID $newPid)."
