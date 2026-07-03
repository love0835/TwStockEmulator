param(
    [switch]$Reset,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$demoDb = Join-Path $root "data\trading_lab_demo.sqlite3"

if ($Reset -or -not (Test-Path -LiteralPath $demoDb)) {
    $seedArgs = @("-Db", $demoDb)
    if ($Reset) {
        $seedArgs += "-Reset"
    }
    & (Join-Path $root "scripts\seed_demo.ps1") @seedArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$env:TW_WATCH_DB_PATH = $demoDb
$env:TW_WATCH_ENABLE_CODEX_LLM = "true"
$env:TW_WATCH_ENABLE_SWING_SELF_CORRECTION = "true"
$env:PYTHONPATH = Join-Path $root "src"
Set-Location $root

if ($Dev) {
    & (Join-Path $root ".venv\Scripts\python.exe") -m tw_watchdesk
    exit $LASTEXITCODE
}

$exe = Join-Path $root "dist\TwWatchDesk.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "找不到 $exe，請先執行 scripts\build_exe.ps1，或改用 scripts\run_demo.ps1 -Dev"
}

Start-Process -FilePath $exe -WorkingDirectory $root
Write-Host "Demo mode started with DB: $demoDb"
