# Builds the 9 images (tag 1.0) from lab3/docker/<service>/Dockerfile.
#   .\lab3\scripts\build-images.ps1             -> local Docker only (for docker compose)
#   .\lab3\scripts\build-images.ps1 -Minikube   -> also loads them into Minikube (no registry needed;
#                                                 works with the containerd runtime Minikube uses)
param([switch]$Minikube)
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\..\.."
$services = "auth-service", "user-service", "catalog-service", "inventory-service", "cart-service",
            "order-service", "payment-service", "notification-service", "gateway"

Push-Location $root
try {
    foreach ($s in $services) {
        Write-Host "==> build libraria/${s}:1.0" -ForegroundColor Cyan
        docker build -f "lab3/docker/$s/Dockerfile" -t "libraria/${s}:1.0" .
        if ($LASTEXITCODE -ne 0) { throw "build of $s failed" }
    }
    docker images "libraria/*" --format "table {{.Repository}}:{{.Tag}}`t{{.Size}}"
} finally { Pop-Location }

if ($Minikube) {
    $mk = (Get-Command minikube -ErrorAction SilentlyContinue).Source
    if (-not $mk) { $mk = "C:\Program Files\Kubernetes\Minikube\minikube.exe" }
    foreach ($s in $services) {
        Write-Host "==> load libraria/${s}:1.0 into Minikube" -ForegroundColor Cyan
        & $mk image load "libraria/${s}:1.0" --overwrite=true
        if ($LASTEXITCODE -ne 0) { throw "loading $s into Minikube failed" }
    }
    & $mk image ls | Select-String "libraria"
}
