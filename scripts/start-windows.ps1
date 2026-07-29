$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
  throw "缺少 Windows Python 环境，请先运行 scripts\setup-windows.ps1。"
}
if (-not (Test-Path "node_modules")) {
  throw "缺少网页依赖，请先运行 scripts\setup-windows.ps1。"
}

$Backend = Start-Process -PassThru -NoNewWindow ".venv\Scripts\python.exe" -ArgumentList @(
  "-m", "uvicorn", "--app-dir", "backend", "app.main:app",
  "--host", "0.0.0.0", "--port", "8000"
)

try {
  Write-Host "后台：http://127.0.0.1:8000"
  Write-Host "网页：http://127.0.0.1:3000"
  npm run start:lan
}
finally {
  if (-not $Backend.HasExited) { Stop-Process -Id $Backend.Id }
}
