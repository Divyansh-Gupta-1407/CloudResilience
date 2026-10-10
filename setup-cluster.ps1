# Creates the kind sandbox cluster and deploys Online Boutique into namespace "boutique".
# Prereqs: Docker Desktop running, kind/kubectl on PATH.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$cluster = 'chaos-sandbox'
$ns = 'boutique'

docker info *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop and retry.' }

if (-not ((kind get clusters) -contains $cluster)) {
    kind create cluster --name $cluster --wait 120s
}
kubectl config use-context "kind-$cluster"

kubectl create namespace $ns --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n $ns -f "$root\microservices-demo\release\kubernetes-manifests.yaml"
& "$root\patches\apply-frontend-patches.ps1"

Write-Host 'Waiting for all deployments to become available (first pull can take ~5-10 min)...'
kubectl wait -n $ns --for=condition=available deployment --all --timeout=900s

# Resilient frontend (graceful degradation, per-call timeouts, fast reconnect). Built locally
# from microservices-demo/src/frontend; switch back with: python patches\use_frontend.py original
if (-not (docker images -q frontend-resilient:v1)) {
    docker build -t frontend-resilient:v1 "$root\microservices-demo\src\frontend"
}
& "$root\.venv\Scripts\python.exe" "$root\patches\use_frontend.py" resilient
kubectl get pods -n $ns

Write-Host "`nFrontend: run 'kubectl port-forward -n $ns svc/frontend 8080:80' then open http://localhost:8080"
