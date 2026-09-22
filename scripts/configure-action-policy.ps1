param(
    [string]$Approver = ""
)

$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Config = Join-Path $Root "config\local.json"
if (-not (Test-Path $Config)) { throw "Missing $Config; run setup-windows.ps1 first." }

$env:WORKBOT_APPROVER = $Approver
@'
import json, os
from pathlib import Path
p=Path('config/local.json')
obj=json.loads(p.read_text(encoding='utf-8-sig'))
approver=os.environ.get('WORKBOT_APPROVER','').strip()
if not approver:
    allowed=((obj.get('access_control') or {}).get('allow_senders') or [])
    approver=str(allowed[0]) if allowed else ''
policy={
  'default':'approve',
  'approver_senders':[approver] if approver else [],
  'approval_timeout_seconds':900,
  'rules':[
    {'match':['read.*','search.*','status.*','windows.fs.read','windows.fs.list','workbot.query','code.edit.workspace','build.run','test.run','git.status','git.diff','git.commit.local'],'decision':'allow'},
    {'match':['git.push','git.merge','git.force_push','git.reset_hard','git.clean.force','deploy.*','release.*','im.send','email.send','calendar.write','cloud.write','fs.delete.*','fs.write.outside_workspace','database.write.*','process.sudo'],'decision':'approve'},
    {'match':['credential.export','secret.exfiltrate','security.disable','system.destructive'],'decision':'deny'},
  ],
}
obj['action_policy']=policy
p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('action_policy configured')
print('approver_senders:', policy['approver_senders'])
'@ | .\.venv\Scripts\python.exe -
