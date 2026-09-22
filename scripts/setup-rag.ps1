param(
    [switch]$DownloadModel
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "WorkBot virtualenv not found: $Python" }

function Invoke-PythonSnippet {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    # Do not pipe a PowerShell here-string to `python -`.
    # Windows PowerShell / host encoding can inject U+FEFF (BOM) into stdin.
    # Passing the snippet as the -c argument avoids stdin re-encoding entirely.
    $CleanCode = $Code.TrimStart([char]0xFEFF)
    & $Python -c $CleanCode
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

Write-Host "== Install WorkBot RAG dependencies =="
& $Python -m pip install -e ".[rag]"
if ($LASTEXITCODE -ne 0) {
    throw "pip install WorkBot RAG dependencies failed with exit code $LASTEXITCODE"
}

Write-Host "== sqlite-vec probe =="
Invoke-PythonSnippet -Name "sqlite-vec probe" -Code @'
import sqlite3
import sqlite_vec

c = sqlite3.connect(":memory:")
c.enable_load_extension(True)
sqlite_vec.load(c)
c.enable_load_extension(False)
print("sqlite=", c.execute("select sqlite_version()").fetchone()[0])
print("sqlite-vec=", c.execute("select vec_version()").fetchone()[0])
'@

if ($DownloadModel) {
    Write-Host "== Download/load Qwen3-Embedding-0.6B =="
    Invoke-PythonSnippet -Name "Qwen3 embedding model load" -Code @'
from sentence_transformers import SentenceTransformer

m = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B")
print("model loaded:", m)
'@
}

Write-Host "WORKBOT_RAG_SETUP_OK"
