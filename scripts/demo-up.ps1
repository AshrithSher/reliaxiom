<#
.SYNOPSIS
  Launch the live demo: the lab (docker compose) + the dashboard. Everything else — starting the
  agent, injecting faults, approving Tier-2 actions — is driven from the dashboard UI.

.DESCRIPTION
  The only terminal role is running the 9-container lab. The dashboard is the single pane of
  glass: click "Start agent" to run the SRE agent (with --execute --jira --confluence), use the
  fault buttons to inject incidents, approve/reject Tier-2 proposals, and watch it all live.

  Pass -Reset to clear prior incident state first.

.EXAMPLE
  .\scripts\demo-up.ps1
  .\scripts\demo-up.ps1 -Reset
#>
param(
  [switch]$Reset,
  [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$config = "configs\demo.json"
$lab = "C:\Mine\Concave\logs-streaming-demo-app\docker-compose.yml"

if (-not (Test-Path $py)) {
  throw "venv python not found at $py — run: python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e `".[dev,dashboard]`""
}

if ($Reset) {
  Write-Host "Resetting agent state (.state)…" -ForegroundColor Yellow
  Remove-Item (Join-Path $repo ".state") -Recurse -Force -ErrorAction SilentlyContinue
}

# 1. The watched system (the only thing that lives in a terminal)
Write-Host "Starting the lab (docker compose)…" -ForegroundColor Cyan
docker compose -f $lab up -d | Out-Host

# 2. The dashboard (own window) — drives the agent + faults + approvals
Write-Host "Starting the dashboard on http://localhost:$Port …" -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "Set-Location '$repo'; `$env:SRE_DASHBOARD_CONFIG='$config'; " +
  "& '$py' -m uvicorn sre_agent.dashboard.server:app --port $Port"
)

Start-Sleep -Seconds 3
Start-Process "http://localhost:$Port"
Write-Host "`nDemo is up. In the dashboard:" -ForegroundColor Green
Write-Host "  1) click 'Start agent'   2) click a fault (e.g. 'Auth tier failing')" -ForegroundColor Green
Write-Host "  3) watch it detect → diagnose → remediate; approve Tier-2 faults inline." -ForegroundColor Green
Write-Host "Tickets land in Jira (HELP) and post-mortems in Confluence (ReliAxiom)." -ForegroundColor Green
