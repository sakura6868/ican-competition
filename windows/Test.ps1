# 工序智传 - Windows 测试（后端 + 前端构建 + 秘密扫描）
# Usage: powershell -ExecutionPolicy Bypass -File Test.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "[Test] backend tests + coverage" -ForegroundColor Cyan
Push-Location (Join-Path $RepoRoot "backend")
try {
    uv run pytest --cov=gongxu --cov-report=term-missing
    uv run bandit -r gongxu -x tests
}
finally {
    Pop-Location
}

Write-Host "[Test] frontend build" -ForegroundColor Cyan
Push-Location (Join-Path $RepoRoot "frontend")
try {
    npm run build
}
finally {
    Pop-Location
}

Write-Host "[Test] all green" -ForegroundColor Green
