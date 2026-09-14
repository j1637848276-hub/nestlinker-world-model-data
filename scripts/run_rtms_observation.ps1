param(
    [string]$Config = "config/rtms-observation.json",
    [string]$StorageRoot = "data/raw/rtms-observations",
    [switch]$IncludeLowFrequency
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

# New scheduler processes do not inherit changes made to the user's environment
# after the desktop app started. Import only this credential, never print it.
if ($env:OS -eq "Windows_NT") {
    $userServiceKey = [Environment]::GetEnvironmentVariable(
        "DATA_GO_KR_SERVICE_KEY", [EnvironmentVariableTarget]::User
    )
    if (-not [string]::IsNullOrWhiteSpace($userServiceKey)) {
        $env:DATA_GO_KR_SERVICE_KEY = $userServiceKey
    }
    Remove-Variable userServiceKey -ErrorAction SilentlyContinue
}
if ([string]::IsNullOrWhiteSpace($env:DATA_GO_KR_SERVICE_KEY)) {
    throw "DATA_GO_KR_SERVICE_KEY is not configured; collection not started"
}

$python = "python"
$venvPython = Join-Path $repoRoot ".venv/Scripts/python.exe"
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
}

$arguments = @(
    "-m", "worldmodel_data", "observe-rtms",
    "--config", $Config,
    "--storage-root", $StorageRoot
)
if ($IncludeLowFrequency) {
    $arguments += "--include-low-frequency"
}

& $python @arguments
exit $LASTEXITCODE
