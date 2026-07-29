$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  py -3 -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install -r "backend\requirements.txt"
npm install
npm run build

Write-Host "首次准备完成。以后双击 启动冷二联轧系统.bat。" -ForegroundColor Green
