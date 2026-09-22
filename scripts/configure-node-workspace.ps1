param(
    [Parameter(Mandatory=$true)][string]$Node,
    [Parameter(Mandatory=$true)][string[]]$Root,
    [int]$ProjectDiscoveryDepth = 2,
    [int]$IndexPathDepth = 5,
    [int]$MaxProjects = 20,
    [int]$MaxFilesPerProject = 1200,
    [int]$MaxChunksPerProject = 160,
    [int]$MaxIndexCharsPerProject = 600000
)

$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
$Config = ".\config\local.json"
if (-not (Test-Path $Config)) { throw "Missing $Config; run setup-windows.ps1 first." }
$obj = Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $obj.nodes) { throw "No nodes configured." }
$NodeName = [string]$Node
$prop = $obj.nodes.PSObject.Properties[$NodeName]
if (-not $prop) { throw "Unknown node '$NodeName'." }

# PowerShell variable names are case-insensitive.  Do not use `$node` here:
# it aliases the `$Node` parameter and would replace the node name string with
# the PSCustomObject.  Keep the name and config in distinct variables.
$nodeCfg = $prop.Value
$nodeCfg | Add-Member -Force -NotePropertyName workspace_knowledge_roots -NotePropertyValue @($Root)
$wk = [ordered]@{
    project_discovery_depth = [Math]::Max(0, $ProjectDiscoveryDepth)
    index_path_depth = [Math]::Max(0, $IndexPathDepth)
    max_projects = [Math]::Max(1, $MaxProjects)
    max_files_per_project = [Math]::Max(20, $MaxFilesPerProject)
    max_chunks_per_project = [Math]::Max(1, $MaxChunksPerProject)
    max_index_chars_per_project = [Math]::Max(10000, $MaxIndexCharsPerProject)
}
$nodeCfg | Add-Member -Force -NotePropertyName workspace_knowledge -NotePropertyValue ([pscustomobject]$wk)
# Explicitly write the updated object back to the named node property.  This
# avoids relying on reference semantics of PSPropertyInfo.Value.
$obj.nodes | Add-Member -Force -NotePropertyName $NodeName -NotePropertyValue $nodeCfg
$obj | ConvertTo-Json -Depth 40 | Set-Content $Config -Encoding UTF8
& .\.venv\Scripts\python.exe .\scripts\compact-config.py .\config\local.json --quiet
if ($LASTEXITCODE -ne 0) { throw "Failed to compact $Config" }

# Re-read the persisted file and fail loudly if the node roots did not survive
# serialization/compaction.  Bounds equal to runtime defaults may intentionally
# be pruned, but roots are never a default and must remain.
$verify = Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$verifyProp = $verify.nodes.PSObject.Properties[$NodeName]
if (-not $verifyProp) { throw "Node '$NodeName' disappeared while saving $Config" }
$persistedRoots = @($verifyProp.Value.workspace_knowledge_roots)
$expectedRoots = @($Root | ForEach-Object { [string]$_ })
if ($persistedRoots.Count -ne $expectedRoots.Count -or (Compare-Object $expectedRoots $persistedRoots)) {
    throw "Remote workspace roots were not persisted for node '$NodeName'. Expected: $($expectedRoots -join ", "); actual: $($persistedRoots -join ", ")"
}

Write-Host "Configured remote workspace knowledge for $NodeName."
Write-Host "- roots: $($Root -join ', ')"
Write-Host "- project discovery depth: $ProjectDiscoveryDepth"
Write-Host "- index path depth: $IndexPathDepth"
Write-Host "- max projects: $MaxProjects"
Write-Host "- max files/project: $MaxFilesPerProject"
Write-Host "- max chunks/project: $MaxChunksPerProject"
Write-Host "- max indexed chars/project: $MaxIndexCharsPerProject"
Write-Host "- persisted roots: $($persistedRoots -join ", ")"
if (-not $verifyProp.Value.workspace_knowledge) {
    Write-Host "- bounds: runtime defaults (omitted from local.json by compaction)"
}
Write-Host "IMPORTANT: redeploy the worker so it receives/enforces the new roots:"
Write-Host "  .\scripts\install-node.ps1 -Node $NodeName"
