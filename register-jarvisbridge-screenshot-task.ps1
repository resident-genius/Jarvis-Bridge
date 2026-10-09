param(
  [string]$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name,
  [string]$TaskName = 'JarvisBridge Screenshot'
)
$ErrorActionPreference = 'Stop'
$userDefault = $User
$script = Join-Path $PSScriptRoot 'jarvisbridge-screenshot-capture.ps1'
$incoming = Join-Path $PSScriptRoot 'proof\incoming'
$execLimitSeconds = 60
try {
  $bridgeSettingsScript = Join-Path $PSScriptRoot 'BridgeSettings.ps1'
  if (Test-Path -LiteralPath $bridgeSettingsScript) {
    . $bridgeSettingsScript
    if (-not $PSBoundParameters.ContainsKey('User')) { $User = Get-BridgeSetting 'scheduled_tasks.run_as_user' $User }
    if (-not $PSBoundParameters.ContainsKey('TaskName')) { $TaskName = Get-BridgeSetting 'scheduled_tasks.screenshot.name' $TaskName }
    $script = Expand-BridgePath (Get-BridgeSetting 'paths.screenshot_capture_script' $script)
    $incoming = Expand-BridgePath (Get-BridgeSetting 'paths.screenshot_incoming_dir' $incoming)
    $execLimitSeconds = Get-BridgeSetting 'scheduled_tasks.screenshot.execution_time_limit_seconds' $execLimitSeconds
  }
} catch { }
if (-not $User) { $User = $userDefault }
if (-not (Test-Path -LiteralPath $incoming)) { New-Item -ItemType Directory -Path $incoming -Force | Out-Null }
$pwsh = (Get-Command powershell.exe).Source
$action = New-ScheduledTaskAction -Execute $pwsh -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $script) -WorkingDirectory $incoming
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds $execLimitSeconds) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings -Description 'Jarvis Bridge real desktop screenshot. Interactive user only. Started on demand: schtasks /run /tn "JarvisBridge Screenshot"' | Out-Null
$t = Get-ScheduledTask -TaskName $TaskName
Write-Output ("REGISTERED name={0} state={1} user={2} logon={3}" -f $t.TaskName, $t.State, $t.Principal.UserId, $t.Principal.LogonType)
