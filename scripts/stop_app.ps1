[CmdletBinding(SupportsShouldProcess)]
param()
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot "data\runtime\app.pid"
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Output "No app PID file."; exit 0 }
$appPid = [int](Get-Content -LiteralPath $pidPath)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$appPid"
if (-not $process) { Write-Output "App already stopped."; exit 0 }
if ($process.CommandLine -notmatch "uvicorn.*app\.main:app") {
    throw "PID was reused by another program; refusing to stop it."
}
# Windows venv python.exe is a redirector: stopping only that parent can leave
# the actual Uvicorn child listening. Stop only its identical-command child,
# never unrelated Python, Chroma, corpus builds, translators or other descendants.
$children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$appPid" |
    Where-Object { $_.CommandLine -eq $process.CommandLine })
foreach ($target in ($children + @($process))) {
    $targetId = [int]$target.ProcessId
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$targetId"
    if ($current -and $current.CommandLine -eq $target.CommandLine -and
        $PSCmdlet.ShouldProcess("Project Uvicorn PID $targetId", "Stop")) {
        Stop-Process -Id $targetId -Force -ErrorAction Stop
        Wait-Process -Id $targetId -Timeout 10 -ErrorAction SilentlyContinue
    }
}
Write-Output "API stop handled for registered PID $appPid. Chroma and source data are unchanged."
