<#
  Invoke-JarvisTask_timeout_test.ps1 -- validates the runner's timeout result.
  Loads the functions from Invoke-JarvisTask.ps1 via the AST (the watch loop never
  runs), puts a fake cursor-agent.ps1 first on PATH, and runs the real Invoke-Task:
    * a hanging agent with a 3-second limit -> timeout result JSON
    * a hanging agent with huge output      -> log tail truncated, noted in report
    * a fast agent with a JARVIS REPORT     -> success result unchanged
  Then feeds the timeout result through bridge_core Loop B (FakeSMTP, no real mail).
  Run:  powershell -ExecutionPolicy Bypass -File .\Invoke-JarvisTask_timeout_test.ps1
#>
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:pass = 0; $script:fail = 0
function Check($name, $cond, $detail = '') {
  if ($cond) { $script:pass++; Write-Host "PASS  $name" }
  else { $script:fail++; Write-Host "FAIL  $name  $detail" -ForegroundColor Red }
}

$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $here 'Invoke-JarvisTask.ps1'), [ref]$null, [ref]$null)
foreach ($fn in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
  . ([scriptblock]::Create($fn.Extent.Text))
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-timeout-test-" + [guid]::NewGuid().ToString('N'))
$Inbox = Join-Path $tmp 'inbox'; $doneDir = Join-Path $Inbox 'done'; $logDir = Join-Path $Inbox 'logs'
$allowFile = Join-Path $Inbox 'allowed-workspaces.txt'
$ws = Join-Path $tmp 'ws'; $bin = Join-Path $tmp 'bin'
foreach ($d in @($Inbox, $doneDir, $logDir, $ws, $bin)) { New-Item -ItemType Directory -Path $d | Out-Null }
Set-Content $allowFile $ws
$AutoApprove = $true
$TimeoutMinutes = 0.05   # 3 seconds
$oldPath = $env:PATH
$env:PATH = "$bin;$env:PATH"

function Set-FakeAgent($body) { Set-Content -LiteralPath (Join-Path $bin 'cursor-agent.ps1') -Value $body -Encoding UTF8 }
function New-Task($id) {
  $p = Join-Path $Inbox "$id.jarvis-task.json"
  @{ task_id = $id; workspace = $ws; goal = 'test'; acceptance_criteria = @('x') } | ConvertTo-Json | Set-Content $p -Encoding UTF8
  return $p
}
function Read-Result($id) { Get-Content (Join-Path $Inbox "$id.jarvis-result.json") -Raw | ConvertFrom-Json }

try {
  # (a) simulated timeout: 120 lines of output, then hang
  Set-FakeAgent @'
1..120 | ForEach-Object { "fake-out-line-{0:D3}" -f $_ }
Write-Error 'fake-err-line'
Start-Sleep -Seconds 120
'@
  Invoke-Task (New-Task 'timeout-sim-001')
  $r = Read-Result 'timeout-sim-001'
  Check 'timeout: result JSON written with task_id' ($r.task_id -eq 'timeout-sim-001')
  Check 'timeout: timed_out is true' ($r.timed_out -eq $true)
  Check 'timeout: exit_code is non-zero' ($r.exit_code -ne 0) "exit_code=$($r.exit_code)"
  Check 'timeout: jarvis_report says it timed out, in plain words' ($r.jarvis_report -match 'timed out\. The runner stopped it after (less than a minute|about \d+ minutes?)') $r.jarvis_report
  Check 'timeout: jarvis_report says no result was produced' ($r.jarvis_report -match 'no result') $r.jarvis_report
  Check 'timeout: stderr has the last log line' ($r.stderr -match 'fake-out-line-120') $r.stderr
  Check 'timeout: stderr tail includes the agent stderr log' ($r.stderr -match 'fake-err-line') $r.stderr
  Check 'timeout: stderr has only the last ~50 lines' (($r.stderr -match 'fake-out-line-071') -and ($r.stderr -notmatch 'fake-out-line-070')) $r.stderr
  Check 'timeout: stderr tail is small (<= 8.2 KB)' ($r.stderr.Length -le 8200) "len=$($r.stderr.Length)"
  Check 'timeout: small tail is not marked truncated' ($r.jarvis_report -notmatch 'shortened') $r.jarvis_report
  Check 'timeout: package moved to done' (Test-Path (Join-Path $doneDir 'timeout-sim-001.jarvis-task.json'))

  # silent hang: no output at all (the b60-install-qwen-002 case)
  Set-FakeAgent 'Start-Sleep -Seconds 120'
  Invoke-Task (New-Task 'timeout-silent-001')
  $r = Read-Result 'timeout-silent-001'
  Check 'silent timeout: report says it produced no output at all' ($r.jarvis_report -match 'timed out' -and $r.jarvis_report -match 'no output at all') $r.jarvis_report
  Check 'silent timeout: stderr notes empty logs' ($r.stderr -match '\(empty\)') $r.stderr

  # oversized output: tail truncated and noted
  Set-FakeAgent @'
1..60 | ForEach-Object { ('x' * 2000) + "-big-$_" }
Start-Sleep -Seconds 120
'@
  Invoke-Task (New-Task 'timeout-big-001')
  $r = Read-Result 'timeout-big-001'
  Check 'oversized: stderr tail capped (<= 8.2 KB)' ($r.stderr.Length -le 8200) "len=$($r.stderr.Length)"
  Check 'oversized: tail keeps the newest line' ($r.stderr -match 'big-60') ''
  Check 'oversized: jarvis_report notes the truncation' ($r.jarvis_report -match 'shortened to keep this email small') $r.jarvis_report
  Check 'oversized: result file stays small (< 250 KB)' ((Get-Item (Join-Path $Inbox 'timeout-big-001.jarvis-result.json')).Length -lt 250KB)

  # success path unchanged
  $TimeoutMinutes = 1
  Set-FakeAgent @'
'{"result":"done"}'
'## JARVIS REPORT'
'all good'
Write-Error 'normal-stderr'
'@
  Invoke-Task (New-Task 'success-sim-001')
  $r = Read-Result 'success-sim-001'
  Check 'success: timed_out false, exit_code 0' (($r.timed_out -eq $false) -and ($r.exit_code -eq 0)) "timed_out=$($r.timed_out) exit=$($r.exit_code)"
  Check 'success: jarvis_report is the agent report' ($r.jarvis_report -eq 'all good') $r.jarvis_report
  Check 'success: stderr is the raw stderr, no timeout tail' (($r.stderr -match 'normal-stderr') -and ($r.stderr -notmatch 'last 50 lines')) $r.stderr

  # (b) the bridge result-email path picks up the timeout result
  $py = @'
import json, os, shutil, sys, tempfile
sys.path.insert(0, sys.argv[1])
import bridge_core_test as t
src = sys.argv[2]
t.FakeSMTP.instances.clear()
with tempfile.TemporaryDirectory() as tmp:
    b, cfg, logs = t.make_bridge(tmp, [])
    shutil.copy(src, os.path.join(cfg["results_dir"], os.path.basename(src)))
    n = b.loop_b()
    sent = t.FakeSMTP.instances[-1].sent if t.FakeSMTP.instances else []
    msg = sent[0][2] if sent else ""
    tracked = os.path.basename(src) in open(os.path.join(tmp, "sent.json")).read() if n else False
    print(json.dumps({"emailed": n, "subject_ok": "Subject: [JARVIS-RESULT] timeout-sim-001" in msg,
                      "report_in_body": "timed out" in msg, "size": len(msg), "tracked": tracked}))
'@
  $pyFile = Join-Path $tmp 'loop_b_check.py'
  Set-Content $pyFile $py -Encoding UTF8
  $out = & python $pyFile $here (Join-Path $Inbox 'timeout-sim-001.jarvis-result.json')
  $j = ($out | Select-Object -Last 1) | ConvertFrom-Json
  Check 'loop B: timeout result emailed once' ($j.emailed -eq 1) ($out -join ' ')
  Check 'loop B: subject is [JARVIS-RESULT] timeout-sim-001' $j.subject_ok ($out -join ' ')
  Check 'loop B: email body carries the timeout report' $j.report_in_body ($out -join ' ')
  Check 'loop B: email is small (< 50 KB)' ($j.size -lt 50KB) "size=$($j.size)"
  Check 'loop B: marked sent in sent.json' $j.tracked ($out -join ' ')
} finally {
  $env:PATH = $oldPath
  Get-Job | Where-Object { $_.Name -like 'jarvis-*' } | Remove-Job -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host ""
Write-Host "$script:pass passed, $script:fail failed"
exit ([int]($script:fail -gt 0))
