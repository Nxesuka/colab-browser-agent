# Test de connectivité PC -> Colab (Ollama via Tailscale) — Windows PowerShell
# Usage : .\scripts\check_connection.ps1 [-GpuHost colab-gpu] [-Port 11434] [-Model qwen3-vl:32b]
param(
    [string]$GpuHost = "colab-gpu",
    [int]$Port = 11434,
    [string]$Model = "qwen3-vl:32b"
)
$ErrorActionPreference = "Continue"
$base = "http://${GpuHost}:${Port}"

Write-Host "`n[1/5] Statut Tailscale" -ForegroundColor Cyan
tailscale status

Write-Host "`n[2/5] ping $GpuHost" -ForegroundColor Cyan
ping -n 3 $GpuHost
tailscale ping --c 3 $GpuHost

Write-Host "`n[3/5] Modèles disponibles ($base/api/tags)" -ForegroundColor Cyan
curl.exe -s --max-time 10 "$base/api/tags"

Write-Host "`n`n[4/5] Modèles chargés en VRAM ($base/api/ps)" -ForegroundColor Cyan
curl.exe -s --max-time 10 "$base/api/ps"

Write-Host "`n`n[5/5] Test de génération via l'endpoint OpenAI-compatible (/v1)" -ForegroundColor Cyan
$body = @{
    model    = $Model
    messages = @(@{ role = "user"; content = "Réponds uniquement : OK" })
    max_tokens = 10
} | ConvertTo-Json -Depth 5
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $r = Invoke-RestMethod -Uri "$base/v1/chat/completions" -Method Post -ContentType "application/json; charset=utf-8" `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 300
    Write-Host ("Réponse : " + $r.choices[0].message.content + "  ({0:N1} s)" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
} catch {
    Write-Host "Échec : $($_.Exception.Message)" -ForegroundColor Red
}
