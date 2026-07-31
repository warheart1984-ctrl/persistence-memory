<#
.SYNOPSIS
  Start Continuity Ledger on port 8001 (idempotent).
#>

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Port = if ($env:JARVIS_PORT) { $env:JARVIS_PORT } else { "8001" }
$HostBind = if ($env:JARVIS_HOST) { $env:JARVIS_HOST } else { "127.0.0.1" }
$Base = if ($env:JARVIS_MEMORYBOARD_URL) { $env:JARVIS_MEMORYBOARD_URL.TrimEnd("/") } else { "http://127.0.0.1:$Port" }
$LogDir = Join-Path $Root "data"
$LogFile = Join-Path $LogDir "jarvis.log"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Test-Healthy {
  try {
    $r = Invoke-RestMethod -Uri "$Base/health" -Method GET -TimeoutSec 2
    return ($r.status -eq "ok")
  } catch {
    return $false
  }
}

if (Test-Healthy) {
  Write-Host "[ok] Continuity Ledger already running at $Base"
  exit 0
}

$Python = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
if (-not $Python) {
  Write-Error "No Python found. Install Python 3.11+ or set PATH."
  exit 1
}

Push-Location $Root
try {
  & $Python -c "import fastapi, uvicorn" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[info] Installing package (editable)..."
    & $Python -m pip install -e ".[dev]" --quiet
  }
} finally {
  Pop-Location
}

$ArgList = "-m uvicorn app.main:app --host $HostBind --port $Port --log-level warning"
$ErrLog = Join-Path $LogDir "jarvis.err.log"
Write-Host "[info] Starting Continuity Ledger with $Python"
$proc = Start-Process -FilePath $Python `
  -ArgumentList $ArgList `
  -WorkingDirectory $Root `
  -WindowStyle Minimized `
  -RedirectStandardOutput $LogFile `
  -RedirectStandardError $ErrLog `
  -PassThru

$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
  if (Test-Healthy) {
    Write-Host "[ok] Continuity Ledger live at $Base (pid $($proc.Id))"
    exit 0
  }
  Start-Sleep -Milliseconds 400
}

Write-Error "Service did not become healthy within 20s. Check $LogFile"
exit 1
