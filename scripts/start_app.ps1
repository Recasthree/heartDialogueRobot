param([string]$Python = "", [int]$Port = 8000, [switch]$Restart, [switch]$CheckNetwork)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Python) {
    $localPython = Join-Path $projectRoot ".venv-agentic\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $localPython)) { $localPython = Join-Path $projectRoot ".venv-research\Scripts\python.exe" }
    if (Test-Path -LiteralPath $localPython) { $Python = $localPython }
    else { $Python = (Get-Command python).Source }
}
$version = & $Python -c "import sys; print(int(sys.version_info >= (3,10)))"
if ($version -ne "1") { throw "Python 3.10+ is required; pass -Python with the correct interpreter." }
$deps = Join-Path $projectRoot ".runtime-deps"
if ($Python -like "*.venv-agentic*") { $env:PYTHONPATH = $projectRoot }
elseif (Test-Path -LiteralPath $deps) { $env:PYTHONPATH = "$deps;$projectRoot" }
$env:APP_PORT = "$Port"
$env:MCP_BASE_URL = "http://127.0.0.1:$Port/mcp/"
$env:HF_HOME = Join-Path $projectRoot "data\runtime\hf"
$runtime = Join-Path $projectRoot "data\runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
if ($CheckNetwork) {
    Push-Location $projectRoot
    try { & $Python -m scripts.diagnose_connectivity }
    finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "Connectivity check failed. See the diagnostic output above." }
}
if ($Restart) {
    & (Join-Path $PSScriptRoot "stop_app.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Could not stop the registered API process." }
}
try {
    $existing = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health" -TimeoutSec 2
    if ($existing.status -eq "ok") { Write-Output "App already running: http://127.0.0.1:$Port"; exit 0 }
} catch {}
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) { throw "Port $Port is still occupied; refusing to start a second API. Inspect the owning PID first." }
$process = Start-Process -WindowStyle Hidden -PassThru -FilePath $Python -WorkingDirectory $projectRoot -ArgumentList @(
    "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port", "--no-access-log"
) -RedirectStandardOutput (Join-Path $runtime "app.stdout.log") -RedirectStandardError (Join-Path $runtime "app.stderr.log")
$process.Id | Set-Content -LiteralPath (Join-Path $runtime "app.pid")
Write-Output "App starting (PID $($process.Id)): http://127.0.0.1:$Port"
Write-Output "Readiness: http://127.0.0.1:$Port/api/v1/ready"
