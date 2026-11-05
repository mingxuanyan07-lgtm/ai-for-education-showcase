$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $projectRoot 'work\hybrid-tutor\console\process.json'
if (-not (Test-Path -LiteralPath $stateFile)) { throw 'No owned console process record.' }
$state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
if (-not $process) { Write-Output 'Console already stopped.'; exit 0 }
if ($process.StartTime.ToUniversalTime().Ticks -ne ([datetime]$state.started_utc).ToUniversalTime().Ticks) { throw 'Process identity changed; refusing to stop it.' }
$details = Get-CimInstance Win32_Process -Filter "ProcessId=$($state.pid)"
if (-not $details.CommandLine.Contains('scripts/hybrid_tutor/server.py')) { throw 'Process command mismatch; refusing to stop it.' }
Stop-Process -Id $state.pid
Write-Output 'Stopped only the owned review console. Model jobs, if any, remain managed through task cancellation.'
