# Verify WorkBot-assigned CodeAgent session IDs and resume support.
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Cmd = Get-Command codeagent -ErrorAction Stop
$Source = $Cmd.Source
if (-not $Source) { $Source = $Cmd.Path }
$env:WORKBOT_CODEAGENT_COMMAND = $Source

@'
import asyncio, json, uuid
from pathlib import Path
from workbot.agents.codeagent import CodeAgentBackend
cfg=json.load(open('config/local.json', encoding='utf-8')).get('agent', {})
b=CodeAgentBackend(cfg, Path.cwd())
async def main():
    sid=str(uuid.uuid4())
    print('assigned_session_id:', sid)
    first=await b.run('Reply with exactly: WORKBOT_SESSION_CREATED', new_session_id=sid)
    print('create_returncode:', first.returncode)
    print('create_reported_session_id:', first.session_id)
    print('create_output:', first.text[:1000])
    resumed=await b.run('Reply with exactly: WORKBOT_SESSION_RESUMED', session_id=sid)
    print('resume_returncode:', resumed.returncode)
    print('resume_reported_session_id:', resumed.session_id)
    print('resume_output:', resumed.text[:1000])
asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -

Write-Host "WorkBot assigns a UUID itself with --session-id and resumes it with --sessions."
Write-Host "This diagnostic session is intentionally NOT written into the WeLink Conversation database. After the next normal conversational Agent turn handled by WorkBot, /session should show a UUID automatically."
