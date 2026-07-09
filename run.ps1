# Lime MAX Bot launcher for Windows.
# Usage:  ./run.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from template - fill in your kapuchino-api gateway token." -ForegroundColor Yellow
}

$port = 8000
if ($env:PORT) { $port = $env:PORT }

Write-Host "Starting Lime MAX Bot on http://0.0.0.0:$port ..." -ForegroundColor Green
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port $port
