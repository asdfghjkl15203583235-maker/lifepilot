# LifePilot - start backend and frontend together.
# Run from PowerShell at the repo root:
#   .\scripts\start-dev.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"

Write-Host "LifePilot: starting backend on http://localhost:8081 and frontend on http://localhost:3000" -ForegroundColor Cyan

# Backend env
if (-not (Test-Path (Join-Path $Backend ".env"))) {
    Copy-Item (Join-Path $Backend ".env.example") (Join-Path $Backend ".env")
    Write-Host "Created backend/.env from template. Fill in required keys before first real use." -ForegroundColor Yellow
}

# Frontend env
if (-not (Test-Path (Join-Path $Frontend ".env"))) {
    Copy-Item (Join-Path $Frontend ".env.example") (Join-Path $Frontend ".env")
    Write-Host "Created frontend/.env from template." -ForegroundColor Yellow
}

# Backend dependencies
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating backend virtual environment..." -ForegroundColor Cyan
    Push-Location $Backend
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install -e .
    Pop-Location
}

# Frontend dependencies
if (-not (Test-Path (Join-Path $Frontend "node_modules"))) {
    Write-Host "Installing frontend dependencies..." -ForegroundColor Cyan
    Push-Location $Frontend
    & npm install
    Pop-Location
}

# Start backend in a background job unless it is already listening on :8081
$BackendJob = $null
$AlreadyRunning = Get-NetTCPConnection -LocalPort 8081 -ErrorAction SilentlyContinue
if ($AlreadyRunning) {
    Write-Host "Backend already running on :8081; skipping uvicorn." -ForegroundColor Green
} else {
    Write-Host "Starting backend..." -ForegroundColor Cyan
    $BackendJob = Start-Job -ScriptBlock {
        param($Backend, $Python)
        Set-Location $Backend
        & $Python -m uvicorn main:app --host 0.0.0.0 --port 8081
    } -ArgumentList $Backend, $Python
}

# Start frontend in a background job unless it is already listening on :3000
$FrontendJob = $null
$AlreadyRunningFrontend = Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue
if ($AlreadyRunningFrontend) {
    Write-Host "Frontend already running on :3000; skipping next dev." -ForegroundColor Green
} else {
    Write-Host "Starting frontend..." -ForegroundColor Cyan
    $FrontendJob = Start-Job -ScriptBlock {
        param($Frontend)
        Set-Location $Frontend
        & npm run dev
    } -ArgumentList $Frontend
}

Write-Host ""
Write-Host "LifePilot is starting." -ForegroundColor Green
Write-Host "  Frontend: http://localhost:3000"
Write-Host "  LifePilot: http://localhost:3000/life-pilot"
Write-Host "  Backend:  http://localhost:8081"
Write-Host "  API docs: http://localhost:8081/docs"
Write-Host ""
Write-Host "Press any key to stop both services..." -ForegroundColor Yellow
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

if ($BackendJob) {
    Stop-Job $BackendJob -ErrorAction SilentlyContinue
    Remove-Job $BackendJob -Force -ErrorAction SilentlyContinue
}
if ($FrontendJob) {
    Stop-Job $FrontendJob -ErrorAction SilentlyContinue
    Remove-Job $FrontendJob -Force -ErrorAction SilentlyContinue
}
Write-Host "Stopped." -ForegroundColor Cyan