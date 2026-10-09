#Requires -Version 5.1
<#
.SYNOPSIS
  Installs Jarvis Bridge on this Windows PC (user-local, no admin required).
.DESCRIPTION
  Preflight checks, lays down bridge files, runs the first-run config wizard,
  registers the bridge to start at boot, and fires a self-test task.
  Uninstall with Uninstall-JarvisBridge.ps1 in the same folder.
#>
[CmdletBinding()]
param(
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "JarvisBridge"),
  [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot

function Write-Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "  [!!] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }

# ---------- 1. Preflight ----------
Write-Step "Preflight checks"

$os = Get-CimInstance Win32_OperatingSystem
$build = [int]$os.BuildNumber
if ($build -lt 10240) { Fail "Windows 10 or later required (build $build found)." }
Write-Ok "Windows build $build"

$drive = (Get-Item $InstallRoot).PSDrive.Name 2>$null
if (-not $drive) { $drive = $env:SystemDrive.TrimEnd(':') }
$freeGB = [math]::Round((Get-PSDrive $drive).Free / 1GB, 1)
if ($freeGB -lt 1) { Fail "Less than 1 GB free on drive $drive." }
Write-Ok "$freeGB GB free on drive $drive"

try {
  $py = (python --version 2>&1)
  if ($py -notmatch "Python 3") { throw "no python3" }
  Write-Ok "Found $py"
} catch { Fail "Python 3 is required and was not found on PATH." }

try {
  $null = Test-Connection -ComputerName 8.8.8.8 -Count 1 -Quiet -ErrorAction Stop
  Write-Ok "Network reachable"
} catch { Write-Warn "No network — mailbox pairing will fail until you are online." }

# ---------- 2. Lay down files ----------
Write-Step "Installing files to $InstallRoot"

if (Test-Path $InstallRoot) { Write-Warn "Install folder exists — upgrading in place." }
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
$exclude = @("Install-JarvisBridge.ps1", "Uninstall-JarvisBridge.ps1")
Get-ChildItem $ScriptDir -Exclude $exclude | Copy-Item -Destination $InstallRoot -Recurse -Force
Copy-Item (Join-Path $ScriptDir "Uninstall-JarvisBridge.ps1") $InstallRoot -Force
Write-Ok "Bridge files copied"

# ---------- 3. First-run config wizard ----------
Write-Step "Configuration wizard"
& (Join-Path $InstallRoot "tools\FirstRun-Config.ps1") -InstallRoot $InstallRoot -Quiet:$Quiet
if ($LASTEXITCODE -ne 0) { Fail "Configuration wizard did not complete." }
Write-Ok "bridge.config.json written"

# ---------- 4. Register auto-start (boot, not logon) ----------
Write-Step "Registering auto-start"
$taskName = "JarvisBridge"
$startScript = Join-Path $InstallRoot "Start-JarvisBridge.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`" -IfNotRunning"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
try {
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Jarvis Bridge — Gmail-bus task executor" -Force | Out-Null
  Write-Ok "Scheduled task '$taskName' registered (starts at boot)"
} catch {
  Write-Warn "Could not register scheduled task: $($_.Exception.Message)"
  Write-Warn "Start the bridge manually with: powershell -File `"$startScript`""
}

# ---------- 5. Self-test ----------
Write-Step "Self-test"
& (Join-Path $InstallRoot "tools\SelfTest.ps1") -InstallRoot $InstallRoot
if ($LASTEXITCODE -ne 0) {
  Write-Warn "Self-test reported a problem — see output above."
  Write-Host "The bridge is installed but not verified. Re-run tools\SelfTest.ps1 after fixing." -ForegroundColor Yellow
} else {
  Write-Ok "Self-test passed"
}

Write-Step "Done"
Write-Host "Jarvis Bridge is installed at $InstallRoot" -ForegroundColor Green
Write-Host "To uninstall: run Uninstall-JarvisBridge.ps1 from this folder." -ForegroundColor Gray
