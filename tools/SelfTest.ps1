#Requires -Version 5.1
<#
.SYNOPSIS
  Self-test: validates the install without sending real email.
  Checks config, contract pack, executor presence, and Python syntax.
#>
[CmdletBinding()]
param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "JarvisBridge"))

$ErrorActionPreference = "Continue"
$failures = 0
function Check($name, [scriptblock]$test) {
  try { & $test; Write-Host "  [PASS] $name" -ForegroundColor Green }
  catch { Write-Host "  [FAIL] $name : $($_.Exception.Message)" -ForegroundColor Red; $script:failures++ }
}

Write-Host "`nJarvis Bridge self-test" -ForegroundColor Cyan

Check "bridge.config.json exists and parses" {
  $p = Join-Path $InstallRoot "bridge.config.json"
  if (-not (Test-Path $p)) { throw "missing" }
  $c = Get-Content $p -Raw | ConvertFrom-Json
  if (-not $c.bridge_mailbox) { throw "no bridge_mailbox" }
  if ($c.allowed_senders -notcontains $c.bridge_mailbox) { throw "mailbox not in allowed_senders" }
}

Check "contract pack present" {
  foreach ($f in @("contract\CONTRACT.md","contract\task-schema.json","contract\result-schema.json")) {
    if (-not (Test-Path (Join-Path $InstallRoot $f))) { throw "missing $f" }
  }
}

Check "task-schema.json parses" {
  $p = Join-Path $InstallRoot "contract\task-schema.json"
  $null = Get-Content $p -Raw | ConvertFrom-Json
}

Check "executor reachable" {
  $c = Get-Content (Join-Path $InstallRoot "bridge.config.json") -Raw | ConvertFrom-Json
  $cmd = if ($c.executor -eq "openclaw") { "openclaw" } else { "cursor-agent" }
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd not on PATH" }
}

Check "Python files compile" {
  $pyFiles = Get-ChildItem $InstallRoot -Filter "*.py" -Recurse -ErrorAction SilentlyContinue
  $bad = @()
  foreach ($f in $pyFiles) {
    python -m py_compile $f.FullName 2>$null
    if ($LASTEXITCODE -ne 0) { $bad += $f.Name }
  }
  if ($bad.Count -gt 0) { throw ($bad -join ", ") }
}

Write-Host ""
if ($failures -eq 0) { Write-Host "All checks passed." -ForegroundColor Green; exit 0 }
else { Write-Host "$failures check(s) failed." -ForegroundColor Red; exit 1 }
