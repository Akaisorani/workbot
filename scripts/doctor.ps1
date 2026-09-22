param([switch]$Json, [string]$Config = "config/local.json")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
$argsList = @("-m", "workbot.setup.doctor", "--config", $Config)
if ($Json) { $argsList += "--json" }
& $Python @argsList
exit $LASTEXITCODE
