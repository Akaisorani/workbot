param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "WorkBot virtualenv not found: $Python. Run scripts\setup-windows.ps1 first." }

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$Name
    )
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE" }
}

Write-Host "== Install WorkBot image/OCR dependencies =="
& $Python -m pip install -e ".[vision]"
if ($LASTEXITCODE -ne 0) { throw "vision dependency installation failed with exit code $LASTEXITCODE" }

Write-Host "== RapidOCR probe =="
$Code = @'
from rapidocr import RapidOCR
engine = RapidOCR()
print("RapidOCR ready:", type(engine).__name__)
'@
$CleanCode = $Code.TrimStart([char]0xFEFF)
& $Python -c $CleanCode
if ($LASTEXITCODE -ne 0) { throw "RapidOCR probe failed with exit code $LASTEXITCODE" }

Write-Host "WORKBOT_VISION_SETUP_OK"
