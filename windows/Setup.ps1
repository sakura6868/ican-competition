# 工序智传 - Windows 环境安装
# Usage: powershell -ExecutionPolicy Bypass -File Setup.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "[Setup] gongxu-zhichuan Windows setup" -ForegroundColor Cyan

# 1. Python 3.12 via uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[Setup] installing uv ..."
    irm https://astral.sh/uv/install.ps1 | iex
}
uv python install 3.12

# 2. Backend dependencies + migration
Push-Location (Join-Path $RepoRoot "backend")
try {
    uv sync --all-extras
    if (-not (Test-Path (Join-Path $RepoRoot ".env"))) {
        Copy-Item (Join-Path $RepoRoot ".env.example") (Join-Path $RepoRoot ".env")
        Write-Host "[Setup] .env created from .env.example - review secrets before use" -ForegroundColor Yellow
    }
    uv run python -m gongxu.db.migrate
    Write-Host "[Setup] backend ready" -ForegroundColor Green
}
finally {
    Pop-Location
}

# 3. Frontend dependencies
Push-Location (Join-Path $RepoRoot "frontend")
try {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Host "[Setup] Node.js 22 required. Install from https://nodejs.org" -ForegroundColor Red
        exit 1
    }
    npm install
    Write-Host "[Setup] frontend ready" -ForegroundColor Green
}
finally {
    Pop-Location
}

Write-Host "[Setup] done. Run Start.ps1 to launch." -ForegroundColor Green
