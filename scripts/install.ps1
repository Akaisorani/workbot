param([switch]$Quick, [switch]$NonInteractive, [string]$Config = "config/local.json")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    python -m venv .venv
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
}
& $Python -m pip install -e .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($NonInteractive) { Write-Error "-NonInteractive is reserved for a future release."; exit 2 }
$argsList = @("-m", "workbot.setup.wizard", "install", "--config", $Config)
if ($Quick) { $argsList += "--quick" }
& $Python @argsList
exit $LASTEXITCODE
