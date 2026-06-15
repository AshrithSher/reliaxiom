<#
.SYNOPSIS
  Stand up a FREE, LOCAL Kubernetes cluster (kind) for the SRE agent's KubernetesActionBackend.

.DESCRIPTION
  No cloud, no paid services. `kind` runs a real Kubernetes API inside Docker, so the agent's
  remediation path (scoped RBAC, declarative rollout restart, API-confirmed verification) is
  exercised exactly as it would be against a managed cluster — only the nodes are local.

  Steps:
    1. create a kind cluster named `sre-lab`
    2. apply the least-privilege RBAC (deploy/k8s/rbac.yaml)
    3. mint a ServiceAccount token + print the env vars the agent needs

  Prereqs (all free): Docker Desktop, `kind`, `kubectl` on PATH.
    winget install Kubernetes.kind Kubernetes.kubectl

.EXAMPLE
  ./scripts/setup-kind.ps1
#>
[CmdletBinding()]
param(
    [string]$ClusterName = "sre-lab",
    [string]$Namespace   = "lab"
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

foreach ($tool in @("docker", "kind", "kubectl")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool not found on PATH. Install it (free) before running this script."
    }
}

# `kind get clusters` prints "No kind clusters found." to stderr on a fresh machine; with
# $ErrorActionPreference='Stop' that would abort here, so discard stderr and match on stdout.
$existingClusters = (& kind get clusters 2>$null) -join "`n"
if ($existingClusters -notmatch [regex]::Escape($ClusterName)) {
    Write-Host "Creating kind cluster '$ClusterName'..." -ForegroundColor Cyan
    kind create cluster --name $ClusterName
} else {
    Write-Host "kind cluster '$ClusterName' already exists." -ForegroundColor Yellow
}

Write-Host "Applying least-privilege RBAC..." -ForegroundColor Cyan
kubectl --context "kind-$ClusterName" apply -f (Join-Path $repoRoot "deploy/k8s/rbac.yaml")

Write-Host "Minting a ServiceAccount token (1h)..." -ForegroundColor Cyan
$token  = kubectl --context "kind-$ClusterName" -n $Namespace create token sre-agent --duration=1h
$server = kubectl --context "kind-$ClusterName" config view --minify -o jsonpath='{.clusters[0].cluster.server}'

Write-Host "`nKubernetes backend ready. Configure the agent with:" -ForegroundColor Green
Write-Host "  `$env:SRE_KUBE_API_SERVER = '$server'"
Write-Host "  `$env:SRE_KUBE_TOKEN      = '<token printed below>'"
Write-Host "`n$token`n"
Write-Host "Then run with a config that sets action_backend = 'kubernetes'." -ForegroundColor Green
