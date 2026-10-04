param([ValidateRange(1,65535)][int]$FrontendPort = 3000)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if ($FrontendPort -ne 3000) {
    $configuredOrigins = python -c "from config.settings import settings; print(settings.CORS_ORIGINS)"
    if ($LASTEXITCODE -ne 0) { throw 'Could not read API origin configuration' }
    $env:CORS_ORIGINS = "$configuredOrigins,http://localhost:$FrontendPort,http://127.0.0.1:$FrontendPort"
    Write-Output 'If the API is already running, restart it with this CORS_ORIGINS setting before using the alternate dashboard port.'
}
docker compose up -d --no-recreate --wait --wait-timeout 60
if ($LASTEXITCODE -ne 0) { throw 'Docker services did not start' }
python -m scripts.init_topics
if ($LASTEXITCODE -ne 0) { throw 'Kafka topic initialization failed' }
foreach ($module in @('services.ais_ingestion', 'services.maritime_consumer', 'services.ais_history_recorder', 'uvicorn')) {
    $existing = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match [regex]::Escape("-m $module") }
    if (!$existing) {
        $arguments = @('-m', $module)
        if ($module -eq 'uvicorn') {
            if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) { throw 'Port 8000 is occupied; inspect the existing service' }
            $arguments += @('dashboard.backend.main:app', '--host', '127.0.0.1', '--port', '8000')
        }
        Start-Process -FilePath python -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput "$projectRoot/data/demo-$module.log" -RedirectStandardError "$projectRoot/data/demo-$module-error.log"
    }
}
if (!(Test-Path -LiteralPath "$projectRoot/dashboard/frontend/.next/BUILD_ID")) { throw 'Build the frontend first: cd dashboard/frontend; npm run build' }
if (!(Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath node -ArgumentList 'node_modules/next/dist/bin/next','start','--hostname','127.0.0.1','--port',"$FrontendPort" -WorkingDirectory "$projectRoot/dashboard/frontend" -WindowStyle Hidden -RedirectStandardOutput "$projectRoot/data/demo-frontend.log" -RedirectStandardError "$projectRoot/data/demo-frontend-error.log"
}
Write-Output "Dashboard: http://127.0.0.1:$FrontendPort | API: http://127.0.0.1:8000/docs"
Write-Output 'Connect the operator credential from your existing .env using Workspace access. Credentials remain in browser memory.'
