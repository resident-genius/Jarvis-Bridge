<#
.SYNOPSIS
  Saves the PNG proof screenshot that every Jarvis Bridge result must attach.
.DESCRIPTION
  Master copy. The live copy Neo runs is bridge-proof.ps1 next to the
  openclaw-bridge SKILL.md in OpenClaw's workspace.

  1. Tries a real capture of the whole screen (all monitors).
  2. If that fails or comes back blank (for example when OpenClaw runs as a
     background service with no desktop), draws a labeled "rendered proof"
     PNG instead: task id, time, account, the evidence note, and the files in
     the workspace. -SkipScreen goes straight to the rendered PNG.
  Writes only <Workspace>\<Name> and prints PROOF_PNG / PROOF_KIND /
  PROOF_BYTES. Exit code 0 when a PNG was saved, 1 when nothing was saved.
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File bridge-proof.ps1 -Workspace "C:\work\task-001" -TaskId task-001 -Note "Wrote hello.txt; it contains HELLO."
#>
param(
  [Parameter(Mandatory = $true)][string]$Workspace,
  [Parameter(Mandatory = $true)][string]$TaskId,
  [string]$Note = '',
  [string]$NoteFile = '',
  [string]$Name = '',
  [switch]$SkipScreen
)
$ErrorActionPreference = 'Stop'

if ($TaskId -notmatch '^[A-Za-z0-9._-]+$') { Write-Output 'PROOF_ERROR=TaskId may only use letters, digits, dot, dash, underscore'; exit 1 }
if (-not $Name) { $Name = "$TaskId-proof.png" }
if ($Name -notmatch '^[A-Za-z0-9._-]+\.png$') { Write-Output 'PROOF_ERROR=Name must be a plain file name ending in .png'; exit 1 }
if (-not (Test-Path -LiteralPath $Workspace -PathType Container)) { Write-Output "PROOF_ERROR=workspace folder not found: $Workspace"; exit 1 }
$ws = (Resolve-Path -LiteralPath $Workspace).ProviderPath
$out = Join-Path $ws $Name

Add-Type -AssemblyName System.Drawing, System.Windows.Forms

function Test-Blank([System.Drawing.Bitmap]$bmp) {
  $first = $null
  foreach ($i in 1..8) {
    foreach ($j in 1..8) {
      $c = $bmp.GetPixel([int]($bmp.Width * $i / 9), [int]($bmp.Height * $j / 9)).ToArgb()
      if ($null -eq $first) { $first = $c } elseif ($c -ne $first) { return $false }
    }
  }
  return $true
}

function Save-ScreenCapture {
  try {
    Add-Type -Namespace BridgeProof -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -ErrorAction SilentlyContinue
    [BridgeProof.Dpi]::SetProcessDPIAware() | Out-Null
  } catch { }
  $r = [System.Windows.Forms.SystemInformation]::VirtualScreen
  if ($r.Width -le 0 -or $r.Height -le 0) { throw 'no screen available' }
  $bmp = New-Object System.Drawing.Bitmap $r.Width, $r.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  try {
    $g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
    if (Test-Blank $bmp) { throw 'screen capture came back blank (no visible desktop)' }
    $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  } finally { $g.Dispose(); $bmp.Dispose() }
}

function Split-Wrapped([string]$text, [int]$width) {
  foreach ($line in ($text -split "`r?`n")) {
    if ($line.Length -eq 0) { ''; continue }
    for ($k = 0; $k -lt $line.Length; $k += $width) { $line.Substring($k, [Math]::Min($width, $line.Length - $k)) }
  }
}

function Save-RenderedProof([string]$why) {
  $lines = New-Object System.Collections.Generic.List[string]
  $lines.Add("Jarvis Bridge proof - $TaskId")
  $lines.Add('RENDERED PROOF IMAGE - not a screen capture.')
  $lines.Add("Why: $why")
  $lines.Add(("Made: {0}   Account: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'), [Environment]::UserName))
  $lines.Add("Workspace: $ws")
  $lines.Add('')
  $noteText = $Note
  if ($NoteFile) {
    $nf = Join-Path $ws $NoteFile
    if ((Test-Path -LiteralPath $nf -PathType Leaf) -and ((Resolve-Path -LiteralPath $nf).ProviderPath.StartsWith($ws, [StringComparison]::OrdinalIgnoreCase))) {
      $noteText = ($noteText + "`n" + (Get-Content -LiteralPath $nf -Raw -ErrorAction SilentlyContinue)).Trim()
    } else { $noteText = ($noteText + "`n(note file not found inside the workspace: $NoteFile)").Trim() }
  }
  if ($noteText) {
    $lines.Add('Evidence note:')
    foreach ($l in (Split-Wrapped $noteText 140 | Select-Object -First 60)) { $lines.Add('  ' + $l) }
    $lines.Add('')
  }
  $lines.Add('Files in the workspace (newest first):')
  $files = @(Get-ChildItem -LiteralPath $ws -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -ne $out } | Sort-Object LastWriteTime -Descending)
  if ($files.Count -eq 0) { $lines.Add('  (none)') }
  foreach ($f in ($files | Select-Object -First 40)) {
    $lines.Add(("  {0:yyyy-MM-dd HH:mm:ss}  {1,10:N0} bytes  {2}" -f $f.LastWriteTime, $f.Length, $f.FullName.Substring($ws.Length).TrimStart('\')))
  }
  if ($files.Count -gt 40) { $lines.Add("  ... and $($files.Count - 40) more") }

  $font = New-Object System.Drawing.Font('Consolas', 11)
  $bold = New-Object System.Drawing.Font('Consolas', 13, [System.Drawing.FontStyle]::Bold)
  $lh = 20; $pad = 16
  $bmp = New-Object System.Drawing.Bitmap 1400, ($pad * 2 + $lh * ($lines.Count + 1))
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  try {
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::White)
    $g.FillRectangle([System.Drawing.Brushes]::DarkSlateBlue, 0, 0, $bmp.Width, $pad + $lh + 6)
    $g.DrawString($lines[0], $bold, [System.Drawing.Brushes]::White, $pad, $pad - 6)
    for ($n = 1; $n -lt $lines.Count; $n++) {
      $g.DrawString($lines[$n], $font, [System.Drawing.Brushes]::Black, $pad, $pad + $lh * ($n + 0.5))
    }
    $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  } finally { $g.Dispose(); $bmp.Dispose(); $font.Dispose(); $bold.Dispose() }
}

$kind = 'screen'
try {
  if ($SkipScreen) { throw 'screen capture skipped (-SkipScreen)' }
  Save-ScreenCapture
} catch {
  $kind = 'rendered'
  $why = $_.Exception.Message
  try { Save-RenderedProof $why } catch { Write-Output "PROOF_ERROR=screen capture failed ($why) and rendering failed ($($_.Exception.Message))"; exit 1 }
  Write-Output "PROOF_NOTE=screen capture failed: $why"
}
$len = (Get-Item -LiteralPath $out).Length
if ($len -le 0) { Write-Output "PROOF_ERROR=PNG is empty: $out"; exit 1 }
Write-Output "PROOF_PNG=$out"
Write-Output "PROOF_KIND=$kind"
Write-Output "PROOF_BYTES=$len"
exit 0
