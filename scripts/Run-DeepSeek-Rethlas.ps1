[CmdletBinding()]
param(
    [string]$ProblemFile,
    [string]$RequirementsFile,
    [string]$UserPromptFile,
    [string]$QuestionId = "",
    [string[]]$Tags = @(),
    [ValidateRange(1, 3)][int]$MaxRounds = 3,
    [ValidateRange(1, 900)][int]$TimeoutSeconds = 900,
    [ValidateSet("legacy", "knowledge-route-v1")][string]$TeachingContract = "legacy",
    [ValidateSet("legacy", "verified-v2")][string]$ArchitectureProfile = "legacy",
    [ValidateRange(1, 10)][int]$MaxModelCalls = 10,
    [ValidateRange(1, 12)][int]$MaxToolCalls = 12,
    [ValidateRange(1, 3600)][int]$MaxTotalSeconds = 3600,
    [ValidateRange(1, 500000)][int]$MaxObservedTokens = 500000,
    [string]$ProjectRoot = "",
    [switch]$Preflight,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path $PSScriptRoot -Parent }
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$pythonPath = Join-Path $resolvedRoot ".venv\Scripts\python.exe"
$cliPath = Join-Path $PSScriptRoot "deepseek_rethlas_cli.py"
$cliArguments = @($cliPath, "--project-root", $resolvedRoot)
if ($Preflight) {
    $cliArguments += "preflight"
} else {
    foreach ($requiredValue in @($ProblemFile, $RequirementsFile, $UserPromptFile)) {
        if ([string]::IsNullOrWhiteSpace($requiredValue)) { throw "Supply ProblemFile, RequirementsFile and UserPromptFile." }
    }
    $cliArguments += @("run", "--problem-file", $ProblemFile, "--requirements-file", $RequirementsFile,
                      "--user-prompt-file", $UserPromptFile, "--max-rounds", "$MaxRounds",
                      "--timeout-seconds", "$TimeoutSeconds")
    if ($QuestionId) { $cliArguments += @("--question-id", $QuestionId) }
    if ($PSBoundParameters.ContainsKey("TeachingContract")) {
        $cliArguments += @("--teaching-contract", $TeachingContract)
    }
    if ($PSBoundParameters.ContainsKey("ArchitectureProfile")) {
        $cliArguments += @("--architecture-profile", $ArchitectureProfile)
    }
    foreach ($entry in @(@("MaxModelCalls", "--max-model-calls", $MaxModelCalls),
                         @("MaxToolCalls", "--max-tool-calls", $MaxToolCalls),
                         @("MaxTotalSeconds", "--max-total-seconds", $MaxTotalSeconds),
                         @("MaxObservedTokens", "--max-observed-tokens", $MaxObservedTokens))) {
        if ($PSBoundParameters.ContainsKey($entry[0])) { $cliArguments += @($entry[1], "$($entry[2])") }
    }
    foreach ($tag in $Tags) { $cliArguments += @("--tag", $tag) }
    if ($DryRun) { $cliArguments += "--dry-run" }
}
& $pythonPath -X utf8 @cliArguments
exit $LASTEXITCODE
