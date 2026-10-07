# Forward arguments to the supported local interpreter, never the old Python 3.8.
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonExe = Join-Path $projectRoot ".venv-agentic\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = Join-Path $projectRoot ".venv-research\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $pythonExe)) { $pythonExe = (Get-Command python).Source }
$supported = & $pythonExe -c "import sys; print(int((3,10) <= sys.version_info[:2] < (3,14)))"
if ($supported -ne "1") { throw "Create .venv-research using Python 3.10-3.13 first." }
$deps = Join-Path $projectRoot ".runtime-deps"
if ($pythonExe -like "*.venv-agentic*") { $env:PYTHONPATH = $projectRoot }
elseif (Test-Path -LiteralPath $deps) { $env:PYTHONPATH = "$deps;$projectRoot" }
$env:HF_HOME = Join-Path $projectRoot "data\runtime\hf"
$env:PYTHONIOENCODING = "utf-8"
Push-Location -LiteralPath $projectRoot
try { & $pythonExe @args; $resultCode = $LASTEXITCODE }
finally { Pop-Location }
exit $resultCode
