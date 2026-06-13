<#
.SYNOPSIS
  Tear down the live demo: stop the lab containers (and optionally wipe agent state).

.DESCRIPTION
  Stops the watched system. Close the agent and dashboard windows yourself (Ctrl+C) — they
  run in separate PowerShell windows. Pass -Reset to also clear the agent's persisted state,
  and -Hard to remove the lab containers/volumes entirely.

.EXAMPLE
  .\scripts\demo-down.ps1
  .\scripts\demo-down.ps1 -Reset
  .\scripts\demo-down.ps1 -Hard
#>
param(
  [switch]$Reset,
  [switch]$Hard
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$lab = "C:\Mine\Concave\logs-streaming-demo-app\docker-compose.yml"

if ($Hard) {
  Write-Host "Removing the lab (containers + volumes)…" -ForegroundColor Yellow
  docker compose -f $lab down -v | Out-Host
} else {
  Write-Host "Stopping the lab containers…" -ForegroundColor Cyan
  docker compose -f $lab stop | Out-Host
}

if ($Reset) {
  Write-Host "Clearing agent state (.state)…" -ForegroundColor Yellow
  Remove-Item (Join-Path $repo ".state") -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Down. (Close the agent + dashboard windows with Ctrl+C if still open.)" -ForegroundColor Green
