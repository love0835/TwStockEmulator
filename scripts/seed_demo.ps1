param(
    [string]$Db = ".\data\trading_lab_demo.sqlite3",
    [switch]$Reset
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
}

$env:PYTHONPATH = "src"
$argsList = @("-m", "tw_watchdesk.demo_seed", "--scenario", "all", "--db", $Db)
if ($Reset) {
    $argsList += "--reset"
}

& ".\.venv\Scripts\python.exe" @argsList
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Demo DB ready: $Db"
