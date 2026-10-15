[CmdletBinding()]
param([switch]$Reset, [switch]$SkipInstall)

$ErrorActionPreference = 'Stop'
$contestRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$contestDir = [System.IO.Path]::GetFullPath((Join-Path $contestRoot '.contest'))
$expectedPrefix = $contestRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (!$contestDir.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "拒绝操作工作区外路径：$contestDir"
}
if ($Reset -and (Test-Path -LiteralPath $contestDir)) {
    Remove-Item -LiteralPath $contestDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $contestDir | Out-Null
$keyFile = Join-Path $contestDir 'master-key.txt'
if (!(Test-Path -LiteralPath $keyFile)) {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [System.IO.File]::WriteAllText($keyFile, [Convert]::ToHexString($bytes).ToLowerInvariant())
}
$env:GONGXU_DATABASE_URL = 'sqlite:///.contest/data/gongxu.db'
$env:GONGXU_DATA_ROOT = (Join-Path $contestDir 'data')
$env:GONGXU_MASTER_KEY_HEX = (Get-Content -LiteralPath $keyFile -Raw).Trim()
$env:PYTHONUTF8 = '1'
$backend = Join-Path $contestRoot 'backend'
$frontend = Join-Path $contestRoot 'frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
if (!$SkipInstall) {
    if (!(Get-Command uv -ErrorAction SilentlyContinue)) { throw '请先安装 uv。' }
    Push-Location $backend
    try { & uv sync --extra media; if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' } }
    finally { Pop-Location }
    Push-Location $frontend
    try { & npm ci --no-audit --no-fund --cache (Join-Path $contestRoot '.cache\npm'); if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' } }
    finally { Pop-Location }
}
if (!(Test-Path -LiteralPath $python)) { throw '缺少 Python 环境，请去掉 -SkipInstall。' }
Push-Location $backend
try { & $python -m gongxu.contest_seed; if ($LASTEXITCODE -ne 0) { throw '赛场数据准备失败。' } }
finally { Pop-Location }
Push-Location $frontend
try { & npm run build; if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' } }
finally { Pop-Location }
Write-Host '赛场环境已准备完成。运行 scripts\Start-Contest.ps1 启动。'
Write-Host '本地演示账号见 DEMO.md；请勿将这些账号用于公网部署。'
