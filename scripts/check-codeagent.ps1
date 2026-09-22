# Ensure text piped to native programs (python.exe/ssh/etc.) uses UTF-8 without BOM.
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Cmd = Get-Command codeagent -ErrorAction Stop
Write-Host "== PowerShell resolution =="
$Cmd | Format-List Name,CommandType,Source,Path,Definition

$Source = $Cmd.Source
if (-not $Source) { $Source = $Cmd.Path }
$env:WORKBOT_CODEAGENT_COMMAND = $Source

Write-Host "== WorkBot Python resolution =="
@'
import asyncio, json, os
from pathlib import Path
from workbot.agents.codeagent import CodeAgentBackend
cfg=json.load(open("config/local.json", encoding="utf-8"))["agent"]
b=CodeAgentBackend(cfg, Path.cwd())
print("configured/env:", b.command)
print("resolved:", b._resolve_command())
print("launcher:", b._launcher(["-p", "hello", "--skip-safe-check"]))
async def main():
    r=await b.run("hello")
    print("returncode:", r.returncode)
    print("output:", r.text[:1000])
asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
