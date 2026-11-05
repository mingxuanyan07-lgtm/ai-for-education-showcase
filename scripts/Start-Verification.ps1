param(
    [string]$Model = '',
    [ValidateSet('low','medium','high','xhigh','max','ultra')][string]$ReasoningEffort = '',
    [ValidateRange(30,3600)][int]$TimeoutSeconds = 900
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Missing Python environment: $pythonPath" }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
if ($Model) { $env:CODEX_MODEL = $Model }
if ($ReasoningEffort) { $env:CODEX_REASONING_EFFORT = $ReasoningEffort }
$env:CODEX_TIMEOUT_SECONDS = "$TimeoutSeconds"
Write-Host 'Rethlas verifier: http://127.0.0.1:8091 (Ctrl+C to stop)'
& $pythonPath -X utf8 -m uvicorn verification_api:app --app-dir $PSScriptRoot --host 127.0.0.1 --port 8091
exit $LASTEXITCODE
