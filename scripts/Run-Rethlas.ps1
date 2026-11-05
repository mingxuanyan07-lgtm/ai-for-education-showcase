[CmdletBinding()]
param(
    [string]$ProblemFile = "data/windows_smoke.md",
    [ValidateRange(1, 2147483647)]
    [int]$MaxIterations = 3,
    [string]$ProjectRoot = "",
    [string]$Model = "",
    [string]$ReasoningEffort = "",
    [string]$SessionId = "",
    [string]$LogDir = "",
    [string]$PauseFile = "",
    [ValidateRange(1, 900)]
    [int]$TimeoutSeconds = 900,
    [string]$PythonExecutable = "",
    [switch]$DryRun,
    [switch]$NoRecords
)

$ErrorActionPreference = "Stop"
$runnerPath = Join-Path $PSScriptRoot "runner.py"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Missing runner.py beside this launcher: $runnerPath"
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path $PSScriptRoot -Parent
}
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = Join-Path $resolvedProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable not found: $PythonExecutable. Supply -PythonExecutable for your virtual environment."
}

$runnerArguments = @(
    $runnerPath,
    "--project-root", $resolvedProjectRoot,
    "--problem-file", $ProblemFile,
    "--max-iterations", "$MaxIterations",
    "--timeout-seconds", "$TimeoutSeconds"
)
if (-not [string]::IsNullOrWhiteSpace($Model)) {
    $runnerArguments += @("--model", $Model)
}
if (-not [string]::IsNullOrWhiteSpace($ReasoningEffort)) {
    $runnerArguments += @("--reasoning-effort", $ReasoningEffort)
}
if (-not [string]::IsNullOrWhiteSpace($SessionId)) {
    $runnerArguments += @("--session-id", $SessionId)
}
if (-not [string]::IsNullOrWhiteSpace($LogDir)) {
    $runnerArguments += @("--log-dir", $LogDir)
}
if (-not [string]::IsNullOrWhiteSpace($PauseFile)) {
    $runnerArguments += @("--pause-file", $PauseFile)
}
if ($DryRun) {
    $runnerArguments += "--dry-run"
}
if ($NoRecords) {
    $runnerArguments += "--no-records"
}

# The call operator and argument array preserve directories and problem names
# containing spaces. No shell command is assembled from user-provided strings.
& $PythonExecutable @runnerArguments
$runnerExitCode = $LASTEXITCODE
if ($null -eq $runnerExitCode) {
    throw "The Python runner did not report an exit code."
}
exit $runnerExitCode
