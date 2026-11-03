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

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "  AUTONOMOUS SELF-HEALING GITHUB & SECURITY ENGINE   " -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan

Write-Host "[1/5] Verifying self-healing & accessibility packages (tenacity, rich, ruff)..." -ForegroundColor Yellow
& $PyExe -m pip install --quiet streamlit requests pandas bandit pip-audit pre-commit tenacity rich ruff

Write-Host "[2/5] Running Ruff autonomous Python code self-healer..." -ForegroundColor Yellow
& $PyExe -m ruff check --fix autorun.py recruit_agent.py dashboard.py security_guard.py master_self_heal.py

Write-Host "[3/5] Executing master_self_heal.py & autorun.py (SQLite Schema Heal + Global GitHub Sync)..." -ForegroundColor Yellow
& $PyExe .\autorun.py

Write-Host "[4/5] Running Bandit SAST verification on all Python modules..." -ForegroundColor Yellow
& $PyExe -m bandit -r autorun.py dashboard.py recruit_agent.py security_guard.py master_self_heal.py -ll

Write-Host "[5/5] Committing and syncing healed stack to GitHub..." -ForegroundColor Green
git add .gitignore pyproject.toml .pre-commit-config.yaml .github/workflows/security.yml repos.json security_guard.py recruit_agent.py autorun.py dashboard.py master_self_heal.py requirements.txt start.ps1 start.bat self_heal_sync.ps1
$diffStatus = git status --porcelain
if ($diffStatus) {
    $timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss UTC")
    git commit -m "chore(self-heal): autonomous DB schema healer, 59-repo registry sync, and WCAG accessibility tier ($timestamp)"
    git push
    Write-Host "[SUCCESS] Uploaded self-healed stack to GitHub!" -ForegroundColor Green
} else {
    Write-Host "[CLEAN] Repository is already synchronized with GitHub." -ForegroundColor Green
}