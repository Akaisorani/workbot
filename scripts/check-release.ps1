param(
    [switch]$CodeAgent,
    [switch]$RagRuntime
)

$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Assert-CommandSucceeded([string]$Stage) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Stage failed with exit code $LASTEXITCODE"
    }
}

$Python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $cmd = Get-Command python -ErrorAction Stop
    $Python = $cmd.Source
}

Write-Host "== WorkBot release hygiene =="
& $Python .\scripts\check-release.py
Assert-CommandSucceeded "Release hygiene"

Write-Host "== Python compile check =="
& $Python -m compileall -q .\workbot .\linux\workbot_worker
Assert-CommandSucceeded "Python compile check"

Write-Host "== Full regression suite =="
& $Python -m pytest -q
Assert-CommandSucceeded "Full regression suite"

if ($CodeAgent) {
    Write-Host "== Local CodeAgent runtime =="
    & .\scripts\check-codeagent.ps1
    Assert-CommandSucceeded "CodeAgent runtime check"
}

if ($RagRuntime) {
    Write-Host "== Optional RAG runtime dependencies =="
    @'
import sqlite3
import sqlite_vec
import sentence_transformers

db = sqlite3.connect(":memory:")
db.enable_load_extension(True)
sqlite_vec.load(db)
version = db.execute("select vec_version()").fetchone()[0]
print("sqlite-vec runtime:", version)
print("sentence-transformers:", sentence_transformers.__version__)
print("WORKBOT_RAG_RUNTIME_OK")
'@ | & $Python -
    Assert-CommandSucceeded "RAG runtime check"
}

Write-Host "WORKBOT_RELEASE_CHECK_OK"
