param(
    [Parameter(Mandatory=$true)][string]$Node,
    [Parameter(Mandatory=$true)][string]$SshAlias,
    [string]$Description = "",
    [string[]]$Capabilities = @("linux", "codeagent"),
    [string[]]$Labels = @(),
    [string[]]$RoutingHints = @(),
    [int]$MaxConcurrent = 4,
    [int]$Priority = 100,
    [string[]]$WorkspaceKnowledgeRoot = @(),
    [int]$WorkspaceDiscoveryDepth = 2,
    [int]$WorkspaceIndexDepth = 5,
    [int]$WorkspaceMaxProjects = 20,
    [int]$WorkspaceMaxFilesPerProject = 1200,
    [int]$WorkspaceMaxChunksPerProject = 160,
    [switch]$SetDefault,
    [switch]$AllowRpc
)

$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Config = Join-Path $Root "config\local.json"
if (-not (Test-Path $Config)) { throw "Missing $Config; run setup-windows.ps1 first." }
$obj = Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $obj.nodes) { $obj | Add-Member -NotePropertyName nodes -NotePropertyValue ([pscustomobject]@{}) }
$nodeCfg = [ordered]@{
    ssh_alias = $SshAlias
    description = $(if ($Description) { $Description } else { "Linux development node $Node" })
    relay_command = "python3 -u ~/.workbot/worker_main.py relay"
    capabilities = @($Capabilities)
    labels = @($Labels)
    routing_hints = @($RoutingHints)
    max_concurrent = $MaxConcurrent
    priority = $Priority
    enabled = $true
}
# Preserve an existing default notification conversation if present.
$existing = $obj.nodes.PSObject.Properties[$Node]
if ($existing -and $existing.Value.default_conversation_id) {
    $nodeCfg.default_conversation_id = $existing.Value.default_conversation_id
}
# Preserve/optionally configure per-node remote workspace knowledge.  The same
# roots are copied to the Linux worker on install and become its authorization
# boundary for read-only workspace indexing.
if ($WorkspaceKnowledgeRoot.Count -gt 0) {
    $nodeCfg.workspace_knowledge_roots = @($WorkspaceKnowledgeRoot)
    $nodeCfg.workspace_knowledge = [ordered]@{
        project_discovery_depth = [Math]::Max(0, $WorkspaceDiscoveryDepth)
        index_path_depth = [Math]::Max(0, $WorkspaceIndexDepth)
        max_projects = [Math]::Max(1, $WorkspaceMaxProjects)
        max_files_per_project = [Math]::Max(20, $WorkspaceMaxFilesPerProject)
        max_chunks_per_project = [Math]::Max(1, $WorkspaceMaxChunksPerProject)
    }
} elseif ($existing -and $existing.Value.workspace_knowledge_roots) {
    $nodeCfg.workspace_knowledge_roots = @($existing.Value.workspace_knowledge_roots)
    if ($existing.Value.workspace_knowledge) { $nodeCfg.workspace_knowledge = $existing.Value.workspace_knowledge }
}
$obj.nodes | Add-Member -Force -NotePropertyName $Node -NotePropertyValue ([pscustomobject]$nodeCfg)
if ($SetDefault) { $obj.default_node = $Node }
if ($AllowRpc) {
    if (-not $obj.rpc) { $obj | Add-Member -NotePropertyName rpc -NotePropertyValue ([pscustomobject]@{enabled=$true;allowed_nodes=@();allowed_methods=@("windows.fs.read","windows.fs.list","workbot.query");windows_read_roots=@()}) }
    $vals = @($obj.rpc.allowed_nodes)
    if ($vals -notcontains $Node) { $obj.rpc.allowed_nodes = @($vals + $Node) }
}
$obj | ConvertTo-Json -Depth 20 | Set-Content $Config -Encoding UTF8
& .\.venv\Scripts\python.exe .\scripts\compact-config.py .\config\local.json --quiet
Write-Host "Configured node $Node -> ssh $SshAlias"
Write-Host "Capabilities: $($Capabilities -join ', ')"
if ($nodeCfg.workspace_knowledge_roots) {
    Write-Host "Workspace knowledge roots: $(@($nodeCfg.workspace_knowledge_roots) -join ', ')"
    Write-Host "Workspace bounds: discovery=$($nodeCfg.workspace_knowledge.project_discovery_depth), index-depth=$($nodeCfg.workspace_knowledge.index_path_depth), max-projects=$($nodeCfg.workspace_knowledge.max_projects), max-files/project=$($nodeCfg.workspace_knowledge.max_files_per_project), max-chunks/project=$($nodeCfg.workspace_knowledge.max_chunks_per_project)"
}
Write-Host "Deploy with: .\scripts\install-node.ps1 -Node $Node"
