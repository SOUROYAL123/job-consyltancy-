<#
  One-command launcher: sets up Python, runs the automation, opens the dashboard.

    start.bat                      double-click: setup (first time) + run autorun + open dashboard
    start.ps1 -Schedule            also register hourly background automation (Task Scheduler)
    start.ps1 -NoDashboard         only run the automation, do not open the dashboard
#>
param([switch]$Schedule, [switch]$NoDashboard)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Test-Python($exe, $extra) {
    try {
        $out = & $exe @extra --version 2>&1
        return (($LASTEXITCODE -eq 0) -and ("$out" -match "Python 3"))
    } catch { return $false }
}

# 1. virtual environment
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $base = $null
    if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python "py" @("-3"))) { $base = @("py", "-3") }
    elseif ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-Python "python" @())) { $base = @("python") }
    if (-not $base) {
        Write-Host "Python 3 was not found (the Microsoft Store shortcut does not count)." -ForegroundColor Red
        Write-Host "Install it with:  winget install Python.Python.3.12"
        Write-Host "Then close this window, open a new one and run start.bat again."
        exit 1
    }
    Write-Host "Creating virtual environment..."
    $exe = $base[0]
    $rest = @()
    if ($base.Count -gt 1) { $rest = $base[1..($base.Count - 1)] }
    & $exe @rest -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "Could not create the virtual environment." -ForegroundColor Red; exit 1 }
}

# 2. dependencies (reinstalled only when requirements.txt changes)
$req = Join-Path $PSScriptRoot "requirements.txt"
$marker = Join-Path $PSScriptRoot ".venv\.deps_hash"
$hash = (Get-FileHash $req -Algorithm SHA256).Hash
$needInstall = $true
if (Test-Path $marker) { if ((Get-Content $marker -Raw).Trim() -eq $hash) { $needInstall = $false } }
if ($needInstall) {
    Write-Host "Installing dependencies (first run takes a minute)..."
    & $venvPy -m pip install --quiet --disable-pip-version-check -r $req
    if ($LASTEXITCODE -ne 0) { Write-Host "Dependency install failed." -ForegroundColor Red; exit 1 }
    Set-Content -Path $marker -Value $hash
}

# 3. GitHub token sanity check
if ($env:GITHUB_TOKEN -match "your_token") {
    Remove-Item Env:GITHUB_TOKEN
    Write-Host "Ignoring a placeholder GITHUB_TOKEN (it would make GitHub reject every request)." -ForegroundColor Yellow
}
if (-not $env:GITHUB_TOKEN) {
    Write-Host "Tip: no GITHUB_TOKEN set. Without one only 25 repos refresh per run. Save a real one with:" -ForegroundColor Yellow
    Write-Host '     setx GITHUB_TOKEN "ghp_..."   (then open a new window)' -ForegroundColor Yellow
}

# 4. run the automation once
Write-Host "Running automation..."
& $venvPy (Join-Path $PSScriptRoot "autorun.py")
if ($LASTEXITCODE -ne 0) { Write-Host "autorun.py reported a problem. See autorun.log." -ForegroundColor Yellow }

# 5. optional background scheduling
if ($Schedule) {
    $tr = "`"$venvPy`" `"$PSScriptRoot\autorun.py`""
    schtasks /Create /SC HOURLY /TN "RecruitStackAutorun" /TR $tr /F | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Scheduled: automation runs every hour." -ForegroundColor Green }
    else { Write-Host "Could not create the hourly task. Try again from an Administrator PowerShell." -ForegroundColor Yellow }

    $tr2 = "`"$venvPy`" -m streamlit run `"$PSScriptRoot\dashboard.py`" --server.headless true"
    schtasks /Create /SC ONLOGON /TN "RecruitStackDashboard" /TR $tr2 /F | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Scheduled: dashboard starts at login (http://localhost:8501)." -ForegroundColor Green }
    else { Write-Host "Could not create the login task (needs an Administrator PowerShell)." -ForegroundColor Yellow }
}

# 6. dashboard
if (-not $NoDashboard) {
    $cred = Join-Path $env:USERPROFILE ".streamlit\credentials.toml"
    if (-not (Test-Path $cred)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $cred) | Out-Null
        Set-Content -Path $cred -Value "[general]`nemail = `"`""
    }
    Write-Host "Opening dashboard at http://localhost:8501  (Ctrl+C here to stop)" -ForegroundColor Green
    & $venvPy -m streamlit run (Join-Path $PSScriptRoot "dashboard.py") --browser.gatherUsageStats false
}
