$ErrorActionPreference = "Stop"

if (Test-Path ".\.venv\Scripts\python.exe") {
    $PyExe = ".\.venv\Scripts\python.exe"
} elseif (Test-Path "..\..\.venv\Scripts\python.exe") {
    $PyExe = "..\..\.venv\Scripts\python.exe"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PyExe = "py"
} else {
    $PyExe = "python"
}

Write-Host "[1/3] Enforcing Streamlit zero-telemetry & localhost loopback (127.0.0.1)..." -ForegroundColor Cyan
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = "false"
$env:STREAMLIT_SERVER_ADDRESS = "127.0.0.1"
$env:STREAMLIT_SERVER_HEADLESS = "true"

Write-Host "[2/3] Running autorun.py (GitHub stack audit + PII-sanitized candidate scoring)..." -ForegroundColor Cyan
& $PyExe .\autorun.py

Write-Host "[3/3] Launching Zero-Trust Streamlit Console on http://127.0.0.1:8501 ..." -ForegroundColor Green
if (Get-Command "infisical" -ErrorAction SilentlyContinue) {
    infisical run -- $PyExe -m streamlit run .\dashboard.py --server.address=127.0.0.1 --server.port=8501
} else {
    & $PyExe -m streamlit run .\dashboard.py --server.address=127.0.0.1 --server.port=8501
}