# Deploys Libraria to Minikube: namespace, Secret from lab3/.env, then every manifest.
#   .\lab3\scripts\deploy.ps1
$ErrorActionPreference = "Stop"
$lab3 = Resolve-Path "$PSScriptRoot\.."
$envFile = Join-Path $lab3 ".env"
if (-not (Test-Path $envFile)) { throw "Create lab3/.env from lab3/.env.example first (real secrets, never committed)" }

kubectl apply -f "$lab3/k8s/00-namespace.yaml"
# the Secret is generated from .env every time, so no secret value is ever stored in Git.
# Only secret keys go into it (passwords, JWT key, broker login); plain settings live in the ConfigMap.
$secretArgs = Get-Content $envFile | Where-Object { $_ -match '^\s*(\w*(PASSWORD|SECRET)\w*|RABBITMQ_USER)\s*=' } |
    ForEach-Object { "--from-literal=$($_.Trim())" }
kubectl -n libraria create secret generic shop-secrets @secretArgs --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -k "$lab3/k8s"

Write-Host "Waiting for the pods..." -ForegroundColor Cyan
kubectl -n libraria rollout status statefulset/postgres --timeout=180s
kubectl -n libraria rollout status statefulset/rabbitmq --timeout=240s
foreach ($d in (kubectl -n libraria get deploy -o name)) { kubectl -n libraria rollout status $d --timeout=240s }
kubectl -n libraria get pods -o wide
Write-Host "Storefront: run 'minikube service gateway -n libraria' or 'kubectl -n libraria port-forward svc/gateway 8090:8000'" -ForegroundColor Green
