# Dot-sourced by the bridge scripts. Reads bridge.config.json from this folder
# (or $env:JARVIS_BRIDGE_SETTINGS). Get-BridgeSetting returns -Default -- the
# production value the calling script used before -- whenever the file, the key
# or a value of the same type is missing, so a bad config never stops a script.
$script:BridgeSettingsPath = if ($env:JARVIS_BRIDGE_SETTINGS) { $env:JARVIS_BRIDGE_SETTINGS } else { Join-Path $PSScriptRoot 'bridge.config.json' }
$script:BridgeSettings = $null
$script:BridgeSettingsError = $null
try {
  $script:BridgeSettings = [IO.File]::ReadAllText($script:BridgeSettingsPath) | ConvertFrom-Json
  if ($script:BridgeSettings -isnot [System.Management.Automation.PSCustomObject]) { $script:BridgeSettings = $null; $script:BridgeSettingsError = 'not a JSON object' }
} catch { $script:BridgeSettingsError = $_.Exception.Message }

function Test-BridgeSettingKind($Value, $Default) {
  if ($null -eq $Value) { return $false }
  if ($Default -is [bool]) { return $Value -is [bool] }
  if ($Default -is [int] -or $Default -is [long]) { return ($Value -is [int] -or $Value -is [long]) }
  if ($Default -is [string]) { return $Value -is [string] }
  if ($Default -is [array]) {
    if ($Value -isnot [array]) { return $false }
    foreach ($item in $Value) { if ($item -isnot [string]) { return $false } }
    return $true
  }
  return $false
}

function Get-BridgeSetting([string]$Key, $Default) {
  $node = $script:BridgeSettings
  foreach ($part in $Key.Split('.')) {
    if ($node -isnot [System.Management.Automation.PSCustomObject] -or -not $node.PSObject.Properties[$part]) { return ,$Default }
    $node = $node.$part
  }
  if (Test-BridgeSettingKind $node $Default) { return ,$node }
  return ,$Default
}

# '%USERPROFILE%\x', '%SystemRoot%\x' ... -> absolute path for this machine.
# %BRIDGE_DIR% is the folder holding this file (the bridge install folder).
$script:BridgeDir = $PSScriptRoot
function Expand-BridgePath([string]$Value) {
  $Value = [regex]::Replace($Value, '%BRIDGE_DIR%', { param($m) $script:BridgeDir }, 'IgnoreCase')
  [Environment]::ExpandEnvironmentVariables($Value)
}

# Last path element of a setting ('...\allowed-workspaces.txt' -> 'allowed-workspaces.txt').
function Get-BridgeSettingLeaf([string]$Key, [string]$Default) {
  $leaf = ([string](Get-BridgeSetting $Key $Default)).Split([char[]]@('\', '/')) | Select-Object -Last 1
  if ($leaf) { return $leaf } else { return $Default }
}

# '<inbox>\logs\runner.log' -> 'logs\runner.log' (relative to a runner inbox).
function Get-BridgeInboxRelative([string]$Key, [string]$Default) {
  $value = [string](Get-BridgeSetting $Key ('<inbox>\' + $Default))
  if (-not $value.StartsWith('<inbox>', [StringComparison]::OrdinalIgnoreCase)) { return $Default }
  $rest = $value.Substring(7).TrimStart('\', '/')
  if ($rest) { return $rest } else { return $Default }
}
