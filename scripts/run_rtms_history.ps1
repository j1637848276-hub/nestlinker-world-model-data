param(
    [string]$PlanDir = "data/work/rtms-history-plan/2026-09-14",
    [string]$StorageRoot = "data/raw/rtms-history-observations",
    [int]$RequestBudgetPerApi = 6000
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$userServiceKey = [Environment]::GetEnvironmentVariable("DATA_GO_KR_SERVICE_KEY", [EnvironmentVariableTarget]::User)
if (-not [string]::IsNullOrWhiteSpace($userServiceKey)) {
    $env:DATA_GO_KR_SERVICE_KEY = $userServiceKey
}
Remove-Variable userServiceKey -ErrorAction SilentlyContinue
if ([string]::IsNullOrWhiteSpace($env:DATA_GO_KR_SERVICE_KEY)) {
    throw "DATA_GO_KR_SERVICE_KEY is not configured"
}
python scripts/collect_rtms_history.py --plan-dir $PlanDir --storage-root $StorageRoot --request-budget-per-api $RequestBudgetPerApi
exit $LASTEXITCODE
