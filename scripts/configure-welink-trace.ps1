param(
    [switch]$Disable,
    [int]$CliTimeoutSeconds = 90
)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Path = Join-Path $Root "config\local.json"
if (-not (Test-Path $Path)) { throw "Missing $Path" }
$obj = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $obj.im) { throw "config/local.json has no im section" }
$obj.im | Add-Member -NotePropertyName cli_trace -NotePropertyValue (-not $Disable) -Force
$obj.im | Add-Member -NotePropertyName cli_timeout_seconds -NotePropertyValue $CliTimeoutSeconds -Force
$obj | ConvertTo-Json -Depth 20 | Set-Content $Path -Encoding UTF8
& .\.venv\Scripts\python.exe .\scripts\compact-config.py .\config\local.json --quiet
Write-Host "WeLink CLI trace:" (-not $Disable)
Write-Host "WeLink CLI timeout:" $CliTimeoutSeconds "seconds"
Write-Host "Restart WorkBot for the setting to take effect."
