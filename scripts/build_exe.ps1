param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path -LiteralPath $Python)) {
    if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
        python -m venv .venv
    }
    $Python = ".\.venv\Scripts\python.exe"
}

& $Python -m pip install -e ".[build]"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --name TwWatchDesk `
    --onefile `
    --windowed `
    --paths src `
    --hidden-import taishin_sdk `
    --hidden-import fugle_marketdata `
    "src\tw_watchdesk\__main__.py"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --name TwWatchDeskSetup `
    --onefile `
    --windowed `
    --paths src `
    --hidden-import taishin_sdk `
    --hidden-import fugle_marketdata `
    --hidden-import tw_watchdesk.setup_env `
    "src\tw_watchdesk\setup_wizard.py"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Built dist\TwWatchDesk.exe"
Write-Host "Built dist\TwWatchDeskSetup.exe"
