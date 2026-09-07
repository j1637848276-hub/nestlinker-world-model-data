param(
    [string]$Config = "config/rtms-observation.json",
    [string]$StorageRoot = "data/raw/rtms-observations",
    [switch]$IncludeLowFrequency
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

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
