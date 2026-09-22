# Ensure text piped to native programs (python.exe/ssh/etc.) uses UTF-8 without BOM.
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"

if (-not (Test-Path "config\local.json")) {
    Copy-Item "config\workbot.example.json" "config\local.json"
}
# Keep local.json readable by pruning only values that exactly match
# hard-coded runtime defaults. Unknown/user-specific keys are preserved.
& .\.venv\Scripts\python.exe .\scripts\compact-config.py .\config\local.json --quiet

Write-Host "Checking local tools..."
welink-cli --version
codeagent --help | Select-Object -First 5

$obj = Get-Content .\config\local.json -Raw -Encoding UTF8 | ConvertFrom-Json
if ($obj.nodes) {
    foreach ($p in $obj.nodes.PSObject.Properties) {
        if ($p.Value.enabled -eq $false) { continue }
        $alias = [string]$p.Value.ssh_alias
        if ($alias) {
            Write-Host "Checking node $($p.Name) via ssh $alias ..."
            ssh $alias "echo SSH_OK && hostname"
        }
    }
}
Write-Host "Windows setup complete. Use .\scripts\configure-node.ps1 to add/update nodes."
