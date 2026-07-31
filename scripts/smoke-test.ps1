<#
.SYNOPSIS
  Smoke-test Continuity Ledger GET/POST against a live server (API-only).
#>

$ErrorActionPreference = "Stop"
$Base = if ($env:JARVIS_MEMORYBOARD_URL) { $env:JARVIS_MEMORYBOARD_URL.TrimEnd("/") } else { "http://127.0.0.1:8001" }
$Headers = @{}
if ($env:JARVIS_API_KEY) {
  $Headers["Authorization"] = "Bearer $($env:JARVIS_API_KEY)"
}

Write-Host "=== Continuity Ledger smoke test ==="
Write-Host "Base: $Base"

$health = Invoke-RestMethod -Uri "$Base/health" -Method GET -TimeoutSec 5
if ($health.status -ne "ok") { throw "health failed: $($health | ConvertTo-Json -Compress)" }
Write-Host "[ok] GET /health status=$($health.status) schema=$($health.schema) memories=$($health.memory_count)"

$board = Invoke-RestMethod -Uri "$Base/api/jarvis/memory/board" -Method GET -Headers $Headers -TimeoutSec 5
Write-Host "[ok] GET /api/jarvis/memory/board id=$($board.memory_board.board_id)"

$live = Invoke-RestMethod -Uri "$Base/api/jarvis/memory/retrieve?truth_scope=live&limit=5" -Method GET -Headers $Headers -TimeoutSec 5
Write-Host "[ok] GET retrieve live_count=$($live.memories.Count) selections=$($live.selections.Count)"

$body = @{
  content = "Smoke test Continuity Ledger entry at $(Get-Date -Format o)"
  source_agent = "smoke-test.ps1"
  session_id = "smoke-session"
  type = "fact"
  confidence = 0.4
  status = "draft"
  subject = "smoke-test"
  evidence = @(@{ kind = "script"; ref = "scripts/smoke-test.ps1"; note = "automated smoke" })
  tags = @("smoke-test", "persistence-memory")
} | ConvertTo-Json -Depth 5

$created = Invoke-RestMethod -Uri "$Base/api/jarvis/memory" -Method POST -Headers $Headers -Body $body -ContentType "application/json" -TimeoutSec 5
Write-Host "[ok] POST /api/jarvis/memory id=$($created.memory.id) sha=$($created.memory.content_sha256.Substring(0,12))..."
Write-Host "=== ALL SMOKE CHECKS PASSED ==="
