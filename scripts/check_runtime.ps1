param([int]$Port = 8000, [int]$ChromaPort = 8001)
foreach ($url in @("http://127.0.0.1:$Port/api/v1/health", "http://127.0.0.1:$Port/api/v1/ready", "http://127.0.0.1:$ChromaPort/api/v1/heartbeat")) {
    try { Write-Output $url; Invoke-RestMethod -Uri $url -TimeoutSec 5 | ConvertTo-Json -Depth 5 }
    catch { Write-Output "Unavailable: $url" }
}
