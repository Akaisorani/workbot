param(
    [string]$Config = "config/local.json",
    [switch]$NoAutostart,
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "WorkBot virtualenv not found: $Python. Run .\scripts\install.ps1 first."
}
if (-not (Test-Path $Config)) {
    throw "WorkBot config not found: $Config"
}

$GuiArgs = @("-m", "workbot.gui.app", "--config", $Config)
if (-not $NoAutostart) { $GuiArgs += "--autostart" }
if ($Verbose) { $GuiArgs += "--verbose" }

& $Python @GuiArgs
exit $LASTEXITCODE
