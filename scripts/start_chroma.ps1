param(
    [string]$ChromaRoot = "",
    [int]$Port = 8001
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $ChromaRoot) {
    if ($env:CHROMA_ROOT) { $ChromaRoot = $env:CHROMA_ROOT }
    else { $ChromaRoot = Join-Path $projectRoot ".chroma" }
}

$python = Join-Path $ChromaRoot ".venv310\Scripts\chroma.exe"
$data = Join-Path $ChromaRoot "data"
$logs = Join-Path $ChromaRoot "logs"

if (-not (Test-Path $python)) {
    throw "Chroma executable not found: $python"
}

New-Item -ItemType Directory -Force -Path $data, $logs | Out-Null
Start-Process -WindowStyle Hidden -FilePath $python -ArgumentList @(
    "run", "--path", $data, "--host", "127.0.0.1", "--port", "$Port",
    "--log-path", (Join-Path $logs "chroma.log")
) | Out-Null

for ($i = 0; $i -lt 30; $i++) {
    try {
        $heartbeat = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/v1/heartbeat" -UseBasicParsing -TimeoutSec 2
        if ($heartbeat.StatusCode -eq 200) {
            Write-Output "Chroma is ready at http://127.0.0.1:$Port"
            exit 0
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}

throw "Chroma did not become ready; inspect $(Join-Path $logs 'chroma.log')"
