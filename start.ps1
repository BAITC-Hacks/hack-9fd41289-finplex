param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker not found. Install and start Docker Desktop.' }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Docker Engine is not ready. Open Docker Desktop, wait until it is running, and retry.' }
    Write-Host 'Building and starting Finplex. The first build can take several minutes...' -ForegroundColor Cyan
    & docker compose up -d --build --wait --wait-timeout 180
    if ($LASTEXITCODE -ne 0) {
        & docker compose ps
        & docker compose logs --tail 40
        throw 'Startup failed. See the error above. No data volumes were deleted.'
    }
    $mapping = & docker compose port frontend 80
    if ($LASTEXITCODE -ne 0 -or -not $mapping) { throw 'Cannot determine frontend port.' }
    $frontendPort = ([string]($mapping | Select-Object -First 1)).Trim().Split(':')[-1]
    if ($frontendPort -notmatch '^\d+$') { throw 'Unexpected frontend port.' }
    $url = "http://localhost:$frontendPort"
    $ready = $false
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        try {
            $health = Invoke-RestMethod "$url/health" -TimeoutSec 5
            $page = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5
            if ($health.status -eq 'ok' -and $page.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw "Containers started, but the website is not ready. Check: docker compose logs --tail 40" }
    Write-Host "`nFINPLEX IS READY: $url" -ForegroundColor Green
    Write-Host 'Stop: docker compose stop (saved orders are retained).'
    if (-not $NoBrowser) {
        try { Start-Process $url } catch { Write-Warning "Could not open the browser. Open $url manually." }
    }
} catch {
    Write-Host "`nFINPLEX DID NOT START: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally { Pop-Location }
