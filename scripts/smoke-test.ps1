<#
.SYNOPSIS
  Smoke-test Continuity Ledger GET/POST against a live server (API-only).
#>

$ErrorActionPreference = "Stop"
if (-not $env:JARVIS_MEMORYBOARD_URL) {
  throw "JARVIS_MEMORYBOARD_URL is not set. Set it to the ledger you mean, for example http://127.0.0.1:8011 through an SSH tunnel. There is no default address, so nothing is sent (and the API key is never sent) until you choose one."
}
$Base = $env:JARVIS_MEMORYBOARD_URL.TrimEnd("/")
$Headers = @{}
$ApiKey = $env:JARVIS_API_KEY
if (-not $ApiKey -and $env:JARVIS_API_KEY_FILE -and (Test-Path $env:JARVIS_API_KEY_FILE)) {
  $ApiKey = (Get-Content $env:JARVIS_API_KEY_FILE -TotalCount 1).Trim()
}
if ($ApiKey) {
  # Only ever to https or to this machine (e.g. an SSH tunnel to 127.0.0.1).
  $u = [Uri]$Base
  if ($u.Scheme -ne "https" -and $u.Host -notin @("127.0.0.1", "localhost", "[::1]", "::1")) {
    throw "Refusing to send the API key over plain http to a non-loopback host; use https or an SSH tunnel to 127.0.0.1."
  }
  $Headers["X-API-Key"] = $ApiKey
} elseif ($env:JARVIS_ALLOW_UNAUTHENTICATED -notin @("1", "true", "yes", "on")) {
  Write-Warning "Neither JARVIS_API_KEY, JARVIS_API_KEY_FILE nor JARVIS_ALLOW_UNAUTHENTICATED=1 is set; protected routes will 401."
}

Write-Host "=== Continuity Ledger smoke test ==="
Write-Host "Base: $Base"

$health = Invoke-RestMethod -Uri "$Base/health" -Method GET -TimeoutSec 5
if ($health.status -ne "ok") { throw "health failed: $($health | ConvertTo-Json -Compress)" }
Write-Host "[ok] GET /health (liveness) status=$($health.status) schema=$($health.schema)"
$ready = Invoke-RestMethod -Uri "$Base/ready" -Method GET -TimeoutSec 5
if ($ready.status -ne "ready") { throw "not ready: $($ready | ConvertTo-Json -Compress)" }
Write-Host "[ok] GET /ready (readiness) status=$($ready.status)"

$board = Invoke-RestMethod -Uri "$Base/api/jarvis/memory/board" -Method GET -Headers $Headers -TimeoutSec 5
Write-Host "[ok] GET /api/jarvis/memory/board id=$($board.memory_board.board_id)"

$live = Invoke-RestMethod -Uri "$Base/api/jarvis/memory/retrieve?truth_scope=live&limit=5" -Method GET -Headers $Headers -TimeoutSec 5
Write-Host "[ok] GET retrieve live_count=$($live.memories.Count) selections=$($live.selections.Count)"

# Clause V hygiene (partial): prefer decision + evidence over chat/fact dumps.
$body = @{
  content = "Smoke decision: Continuity Ledger API reachable at $(Get-Date -Format o)"
  source_agent = "smoke-test.ps1"
  session_id = "smoke-session"
  type = "decision"
  confidence = 0.4
  status = "draft"
  subject = "smoke-test"
  evidence = @(@{ kind = "script"; ref = "scripts/smoke-test.ps1"; note = "automated smoke" })
  tags = @("smoke-test", "persistence-memory", "clause-v-hygiene")
} | ConvertTo-Json -Depth 5

$created = Invoke-RestMethod -Uri "$Base/api/jarvis/memory" -Method POST -Headers $Headers -Body $body -ContentType "application/json" -TimeoutSec 5
Write-Host "[ok] POST /api/jarvis/memory id=$($created.memory.id) sha=$($created.memory.content_sha256.Substring(0,12))..."
Write-Host "=== ALL SMOKE CHECKS PASSED ==="
