$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not (Test-Path ".\.venv\Scripts\python.exe")) { throw "Missing .venv; run setup-windows.ps1 first." }
& .\.venv\Scripts\python.exe .\scripts\compact-config.py .\config\local.json
