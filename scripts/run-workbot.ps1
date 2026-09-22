# Ensure text piped to native programs (python.exe/ssh/etc.) uses UTF-8 without BOM.
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# PowerShell can resolve command shims (codeagent.cmd/codeagent.ps1) that
# Win32 CreateProcess cannot resolve from the bare name. Pass the exact source
# path to the Python adapter so WorkBot uses the same codeagent as this shell.
$CodeAgent = Get-Command codeagent -ErrorAction Stop
$CodeAgentSource = $CodeAgent.Source
if (-not $CodeAgentSource) { $CodeAgentSource = $CodeAgent.Path }
if (-not $CodeAgentSource) { throw "Get-Command codeagent did not return Source/Path" }
$env:WORKBOT_CODEAGENT_COMMAND = $CodeAgentSource
Write-Host "WorkBot CodeAgent: $CodeAgentSource"

& .\.venv\Scripts\python.exe -m workbot.main --config config\local.json
