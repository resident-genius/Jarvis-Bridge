#Requires -Version 5.1
<#
.SYNOPSIS
  First-run configuration wizard. Collects the install-specific values and
  writes bridge.config.json. Re-running it re-pairs the install.
#>
[CmdletBinding()]
param(
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "JarvisBridge"),
  [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$configPath = Join-Path $InstallRoot "bridge.config.json"

Write-Host "`nJarvis Bridge — first-run setup" -ForegroundColor Cyan
Write-Host "This pairs the bridge with YOUR mailbox. Only email from that" -ForegroundColor Gray
Write-Host "mailbox will ever run tasks on this PC.`n" -ForegroundColor Gray

function Ask($prompt, $default = "") {
  if ($Quiet -and $default -ne "") { return $default }
  $suffix = if ($default -ne "") { " [$default]" } else { "" }
  $ans = Read-Host "$prompt$suffix"
  if ([string]::IsNullOrWhiteSpace($ans)) { return $default }
  return $ans.Trim()
}

# 1. Mailbox
$mailbox = ""
while ($mailbox -notmatch '^[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}$') {
  $mailbox = Ask "Bridge mailbox (the Gmail address tasks come from AND go to)"
  if ($mailbox -notmatch '^[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}$') {
    Write-Host "  That doesn't look like an email address. Try again." -ForegroundColor Yellow
  }
}

# 2. Machine label
$defaultLabel = $env:COMPUTERNAME.ToLower()
$label = Ask "Machine label (used in logs, e.g. 'office-pc')" $defaultLabel
if ($label -notmatch '^[a-z0-9][a-z0-9-]*$') {
  Write-Host "  Label cleaned to lowercase letters/digits/hyphens." -ForegroundColor Yellow
  $label = ($label.ToLower() -replace '[^a-z0-9-]', '-').Trim('-')
  if ([string]::IsNullOrWhiteSpace($label)) { $label = $defaultLabel.ToLower() }
}

# 3. Executor preference
Write-Host "`nWhich AI agent runs tasks on this PC?" -ForegroundColor Cyan
Write-Host "  1) cursor   (default)"
Write-Host "  2) openclaw"
$exChoice = Ask "Choice" "1"
$executor = if ($exChoice -eq "2") { "openclaw" } else { "cursor" }

# 4. Workspace root
$defaultWs = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".jarvis-bridge-tasks"
$wsRoot = Ask "Task workspace folder" $defaultWs

$config = [ordered]@{
  bridge_mailbox   = $mailbox
  machine_label    = $label
  executor         = $executor
  workspace_root   = $wsRoot
  allowed_senders  = @($mailbox)
  check_interval_s = 60
  contract_version = "1.0"
  paired_at        = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

New-Item -ItemType Directory -Force -Path (Split-Path $configPath) | Out-Null
($config | ConvertTo-Json -Depth 4) | Set-Content $configPath -Encoding UTF8

Write-Host "`n  [OK] Config written to $configPath" -ForegroundColor Green
Write-Host "  Mailbox : $mailbox" -ForegroundColor Gray
Write-Host "  Label   : $label" -ForegroundColor Gray
Write-Host "  Executor: $executor" -ForegroundColor Gray

Write-Host "`nNext: the bridge needs your mailbox credentials (app password)." -ForegroundColor Cyan
Write-Host "Create one at https://myaccount.google.com/apppasswords" -ForegroundColor Gray
Write-Host "and save it where the bridge expects it — see README 'Mailbox credentials'.`n" -ForegroundColor Gray
