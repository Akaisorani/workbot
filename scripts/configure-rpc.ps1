param(
    [string[]]$Node = @("linux-dev"),
    [string[]]$ReadRoot = @("C:\example\workbot"),
    [bool]$EnableQuery = $true
)

$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Path = Join-Path $Root "config\local.json"
if (-not (Test-Path $Path)) { throw "Missing config/local.json; run setup-windows.ps1 first" }

$cfg = Get-Content -Raw -Encoding UTF8 $Path | ConvertFrom-Json
$methods = @("windows.fs.read", "windows.fs.list")
if ($EnableQuery) { $methods += "workbot.query" }
$rpc = [PSCustomObject]@{
    enabled = $true
    allowed_nodes = @($Node)
    allowed_methods = @($methods)
    windows_read_roots = @($ReadRoot)
    max_response_chars = 16000
    max_file_chars = 64000
    timeout_seconds = 300
    max_concurrent = 2
}
if ($cfg.PSObject.Properties.Name -contains "rpc") {
    $cfg.rpc = $rpc
} else {
    $cfg | Add-Member -NotePropertyName rpc -NotePropertyValue $rpc
}
$json = $cfg | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Enabled Linux->Windows RPC for nodes: $($Node -join ', ')"
Write-Host "Read roots: $($ReadRoot -join ', ')"
Write-Host "Methods: $($methods -join ', ')"
