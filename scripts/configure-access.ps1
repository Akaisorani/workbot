param(
    [Parameter(Mandatory=$true)][string]$Account,
    [string]$Config = "config/local.json",
    [ValidateSet("allow", "deny")]
    [string]$Default = "deny"
)

$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$ConfigPath = Resolve-Path $Config
$obj = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json

$access = [ordered]@{
    default = $Default
    allow_senders = @($Account)
    deny_senders = @()
    allow_groups = @()
    deny_groups = @()
    log_denied = $true
    reply_denied = $false
}

if ($obj.PSObject.Properties.Name -contains "access_control") {
    $obj.access_control = $access
} else {
    $obj | Add-Member -NotePropertyName access_control -NotePropertyValue $access
}

$json = $obj | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($ConfigPath.Path, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Updated $($ConfigPath.Path)"
Write-Host "Inbound policy: default=$Default; allow sender=$Account; blacklist overrides whitelist."
