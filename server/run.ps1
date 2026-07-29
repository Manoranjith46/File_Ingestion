Param()

Write-Host "Building and starting containers..."
Push-Location -Path $PSScriptRoot
try {
    docker compose build
    docker compose up
} finally {
    Pop-Location
}
