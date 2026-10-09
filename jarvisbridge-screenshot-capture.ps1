$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing, System.Windows.Forms

$outDir = Join-Path $PSScriptRoot 'proof\incoming'
$outDirDefault = $outDir
try {
  $bridgeSettingsScript = Join-Path $PSScriptRoot 'BridgeSettings.ps1'
  if (Test-Path -LiteralPath $bridgeSettingsScript) {
    . $bridgeSettingsScript
    $outDir = Expand-BridgePath (Get-BridgeSetting 'paths.screenshot_incoming_dir' $outDir)
  }
} catch { $outDir = $outDirDefault }
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

$name = $null
if ($args.Count -ge 1 -and $args[0]) { $name = [string]$args[0] }
$req = Join-Path $outDir '_request.txt'
if (-not $name -and (Test-Path -LiteralPath $req)) {
  $name = (Get-Content -LiteralPath $req -Raw).Trim()
}
if (-not $name) { $name = ('capture-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')) }
$name = ($name -replace '[\\/:*?"<>|]', '_')
if ($name -notmatch '\.png$') { $name = $name + '.png' }

$out = Join-Path $outDir $name
$marker = [System.IO.Path]::ChangeExtension($out, '.done')

try {
  Add-Type -Namespace RealCapture -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -ErrorAction SilentlyContinue
  [RealCapture.Dpi]::SetProcessDPIAware() | Out-Null
} catch { }

$r = [System.Windows.Forms.SystemInformation]::VirtualScreen
if ($r.Width -le 0 -or $r.Height -le 0) { throw 'no interactive screen available' }

$bmp = New-Object System.Drawing.Bitmap $r.Width, $r.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
try {
  $g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
} finally { $g.Dispose(); $bmp.Dispose() }

$stamp = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') + ' account=' + [Environment]::UserName + ' session=' + [System.Diagnostics.Process]::GetCurrentProcess().SessionId + " size=$($r.Width)x$($r.Height)"
Set-Content -LiteralPath $marker -Value $stamp -Encoding utf8
if (Test-Path -LiteralPath $req) { Remove-Item -LiteralPath $req -Force -ErrorAction SilentlyContinue }
Write-Output "CAPTURE_OK=$out"
