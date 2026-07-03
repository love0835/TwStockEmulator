$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
}

.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\tw-watchdesk.exe
