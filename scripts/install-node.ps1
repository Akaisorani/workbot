param(
    [Parameter(Mandatory=$true)][string]$Node
)

$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Linux = Join-Path $Root "linux"
$Config = Join-Path $Root "config\local.json"
if (-not (Test-Path $Config)) { throw "Missing $Config" }
$obj = Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$prop = $obj.nodes.PSObject.Properties[$Node]
if (-not $prop) { throw "Node '$Node' is not configured in config/local.json" }
$HostAlias = [string]$prop.Value.ssh_alias
if (-not $HostAlias) { throw "Node '$Node' has no ssh_alias" }
$WorkspaceRoots = @($prop.Value.workspace_knowledge_roots)
$WorkspaceRootsJson = ConvertTo-Json @($WorkspaceRoots) -Compress
$WorkspaceRootsB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($WorkspaceRootsJson))

$RemoteHome = (ssh $HostAlias 'printf "%s" "$HOME"').Trim()
if (-not $RemoteHome) { throw "Could not determine remote HOME for $HostAlias" }
$Remote = "$RemoteHome/.workbot"

Write-Host "Checking required runtime dependencies on $Node ..."
$DepCheck = ssh $HostAlias 'for c in codeagent tmux setsid; do command -v "$c" >/dev/null 2>&1 || { echo "MISSING:$c"; exit 21; }; done; echo dependencies_OK'
if ($LASTEXITCODE -ne 0) { throw "Node '$Node' is missing a required WorkBot runtime dependency (codeagent, tmux or setsid). Remote output: $DepCheck" }
Write-Host $DepCheck

Write-Host "Installing WorkBot worker for node $Node via ssh $HostAlias ..."
ssh $HostAlias "mkdir -p '$Remote' '$Remote/bin' '$Remote/enforced-bin' '$Remote/run' '$Remote/state' '$Remote/logs' '$Remote/docs' '$Remote/skills' '$RemoteHome/.config/systemd/user'"
ssh $HostAlias "systemctl --user disable --now workbot-worker.service >/dev/null 2>&1 || true"

scp -r (Join-Path $Linux "workbot_worker") "${HostAlias}:$Remote/"
scp (Join-Path $Linux "worker_main.py") "${HostAlias}:$Remote/worker_main.py"
scp (Join-Path $Linux "AGENTS.md") "${HostAlias}:$Remote/AGENTS.md"
scp (Join-Path $Linux "config.example.json") "${HostAlias}:$Remote/config.example.json"
ssh $HostAlias "test -f ~/.workbot/config.json || cp ~/.workbot/config.example.json ~/.workbot/config.json"
scp (Join-Path $Linux "bin\agentctl") "${HostAlias}:$Remote/bin/agentctl"
scp -r (Join-Path $Linux "enforced-bin") "${HostAlias}:$Remote/"
scp -r (Join-Path $Linux "docs") "${HostAlias}:$Remote/"
scp -r (Join-Path $Linux "skills") "${HostAlias}:$Remote/"
scp (Join-Path $Linux "systemd\workbot-worker.service") "${HostAlias}:$RemoteHome/.config/systemd/user/workbot-worker.service"

# Preserve all user/node-specific settings while setting the authoritative node name
# and opting legacy installs into per-task CodeAgent cwd isolation.
$RemotePy = @'
import json, pathlib, sys
node=sys.argv[1]
import base64
workspace_roots=json.loads(base64.b64decode(sys.argv[2]).decode('utf-8')) if len(sys.argv)>2 else []
p=pathlib.Path.home()/'.workbot'/'config.json'
obj=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
obj['node_name']=node
obj['workspace_knowledge_roots']=workspace_roots
obj.setdefault('agent_cwd_mode','task')
old='{codeagent} -p "$(cat {prompt})" --skip-safe-check'
if not obj.get('codeagent_new_template') or obj.get('codeagent_new_template') == old:
    obj['codeagent_new_template']='{codeagent} --session-id {session_id} -p "$(cat {prompt})" --skip-safe-check'
obj.setdefault('codeagent_resume_template','{codeagent} --sessions {session_id} -p "$(cat {prompt})" --skip-safe-check')
obj.setdefault('execution_scope_kill_grace_seconds',1.5)
p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
'@
$RemotePy | ssh $HostAlias "python3 - '$Node' '$WorkspaceRootsB64'"

ssh $HostAlias "chmod +x ~/.workbot/bin/agentctl ~/.workbot/enforced-bin/git ~/.workbot/enforced-bin/sudo ~/.workbot/worker_main.py; python3 ~/.workbot/worker_main.py stop || true; rm -f ~/.workbot/run/start.lock; python3 ~/.workbot/worker_main.py ensure; test -S ~/.workbot/run/worker.sock; ~/.workbot/bin/agentctl notify --event worker.install-test --summary '$Node agent-worker installed; task steering, recovery, gated execution and bounded remote workspace indexing available'"
Write-Host "$Node worker installation complete."
if ($WorkspaceRoots.Count -gt 0) { Write-Host "Remote workspace roots enforced on worker: $($WorkspaceRoots -join ', ')" }
