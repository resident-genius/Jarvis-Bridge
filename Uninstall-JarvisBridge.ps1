#Requires -Version 5.1
<#
.SYNOPSIS
  Removes Jarvis Bridge from this PC.
#>
[CmdletBinding()]
param(
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "JarvisBridge"),
  [switch]$Quiet,
  [switch]$RemoveSettings
)

$ErrorActionPreference = "Stop"
function Write-Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

Write-Step "Uninstalling Jarvis Bridge"

$taskName = "JarvisBridge"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
  Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
  Write-Host "  [OK] Scheduled task removed"
}

$procs = Get-Process -Name "python*" -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "$InstallRoot*" }
foreach ($p in $procs) { try { Stop-Process -Id $p.Id -Force } catch {} }
if ($procs) { Write-Host "  [OK] Bridge processes stopped" }

if (-not $RemoveSettings -and -not $Quiet) {
  $ans = Read-Host "Delete your settings and task history too? (y/N)"
  if ($ans -match '^[Yy]') { $RemoveSettings = $true }
}

if (Test-Path $InstallRoot) {
  if ($RemoveSettings) {
    Remove-Item $InstallRoot -Recurse -Force
    Write-Host "  [OK] Install folder and settings removed"
  } else {
    Get-ChildItem $InstallRoot -Exclude "bridge.config.json","workspace" |
      Remove-Item -Recurse -Force
    Write-Host "  [OK] Program files removed (settings kept at $InstallRoot)"
  }
}

Write-Host "`nJarvis Bridge uninstalled." -ForegroundColor Green
