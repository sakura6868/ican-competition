[CmdletBinding()]
param([switch]$Prepare)

$ErrorActionPreference = 'Stop'
$contestRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$contestDir = Join-Path $contestRoot '.contest'
if ($Prepare -or !(Test-Path -LiteralPath (Join-Path $contestDir 'data\gongxu.db'))) {
    & (Join-Path $PSScriptRoot 'Prepare-Contest.ps1') -SkipInstall
}
$env:GONGXU_DATABASE_URL = 'sqlite:///.contest/data/gongxu.db'
$env:GONGXU_DATA_ROOT = (Join-Path $contestDir 'data')
$env:GONGXU_MASTER_KEY_HEX = (Get-Content -LiteralPath (Join-Path $contestDir 'master-key.txt') -Raw).Trim()
$env:PYTHONUTF8 = '1'
$runtime = Join-Path $contestRoot '.runtime\contest'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$python = Join-Path $contestRoot 'backend\.venv\Scripts\python.exe'
$node = (Get-Command node -ErrorAction Stop).Source
$api = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','gongxu.main:app','--host','127.0.0.1','--port','8011') -WorkingDirectory (Join-Path $contestRoot 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'api.log') -RedirectStandardError (Join-Path $runtime 'api-error.log')
$worker = Start-Process -FilePath $python -ArgumentList @('-m','gongxu.worker') -WorkingDirectory (Join-Path $contestRoot 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'worker.log') -RedirectStandardError (Join-Path $runtime 'worker-error.log')
$ui = Start-Process -FilePath $node -ArgumentList @('node_modules/vite/bin/vite.js','preview','--host','127.0.0.1','--port','5185','--strictPort') -WorkingDirectory (Join-Path $contestRoot 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'ui.log') -RedirectStandardError (Join-Path $runtime 'ui-error.log')
@{ api=$api.Id; worker=$worker.Id; ui=$ui.Id } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtime 'processes.json') -Encoding UTF8
Start-Sleep -Seconds 2
Start-Process 'http://127.0.0.1:5185'
Write-Host '赛场版已启动：http://127.0.0.1:5185'
Write-Host '停止服务请运行 scripts\Stop-Contest.ps1'
