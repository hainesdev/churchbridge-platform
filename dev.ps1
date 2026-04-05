<#
.SYNOPSIS
    ChurchBridge AI - dev process manager

.PARAMETER Action
    start   - kill stale processes then start server + client (default)
    stop    - kill all running processes
    restart - stop then start
    status  - show what is running on the dev ports

.EXAMPLE
    .\dev.ps1
    .\dev.ps1 stop
    .\dev.ps1 restart
    .\dev.ps1 status
#>

param(
    [ValidateSet("start","stop","restart","status")]
    [string]$Action = "start"
)

$ServerPort  = 8000
$ClientPort  = 3000
$ProjectRoot = $PSScriptRoot
$PythonExe   = Join-Path $ProjectRoot "server\.venv\Scripts\python.exe"

function Get-PortProcess($port) {
    $lines = netstat -ano | Select-String ":$port\s" | Select-String "LISTENING"
    if (-not $lines) { return $null }
    $procId = ($lines[0].ToString().Trim() -split '\s+')[-1]
    try { return Get-Process -Id ([int]$procId) -ErrorAction Stop }
    catch { return $null }
}

function Stop-Port($port, $label) {
    $proc = Get-PortProcess $port
    if ($proc) {
        Write-Host "  Stopping $label (PID $($proc.Id) on :$port)..." -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 600
        Write-Host "  Stopped." -ForegroundColor Green
    } else {
        Write-Host "  $label not running on :$port" -ForegroundColor DarkGray
    }
}

function Show-Status {
    Write-Host ""
    Write-Host "Process status:" -ForegroundColor Cyan
    $serverProc = Get-PortProcess $ServerPort
    if ($serverProc) {
        Write-Host "  [RUNNING] Server  :$ServerPort  - $($serverProc.Name) (PID $($serverProc.Id))" -ForegroundColor Green
    } else {
        Write-Host "  [STOPPED] Server  :$ServerPort" -ForegroundColor DarkGray
    }
    $clientProc = Get-PortProcess $ClientPort
    if ($clientProc) {
        Write-Host "  [RUNNING] Client  :$ClientPort  - $($clientProc.Name) (PID $($clientProc.Id))" -ForegroundColor Green
    } else {
        Write-Host "  [STOPPED] Client  :$ClientPort" -ForegroundColor DarkGray
    }
    Write-Host ""
}

function Start-Server {
    if (-not (Test-Path $PythonExe)) {
        Write-Host "  ERROR: venv not found at $PythonExe" -ForegroundColor Red
        Write-Host "  Run: python -m venv server\.venv  then  pip install -r server\requirements.txt" -ForegroundColor Yellow
        return
    }
    Write-Host "  Starting API server on :$ServerPort..." -ForegroundColor Cyan
    $args = @("-m","uvicorn","server.main:app","--reload","--host","127.0.0.1","--port","$ServerPort","--log-level","info")
    Start-Process -FilePath $PythonExe -ArgumentList $args -WorkingDirectory $ProjectRoot -NoNewWindow
    Write-Host "  Server started." -ForegroundColor Green
}

function Start-Client {
    $npmPath = (Get-Command npm -ErrorAction SilentlyContinue)
    if (-not $npmPath) {
        Write-Host "  WARNING: npm not found - skipping client" -ForegroundColor Yellow
        return
    }
    Write-Host "  Starting Next.js client on :$ClientPort..." -ForegroundColor Cyan
    $clientDir = Join-Path $ProjectRoot "client"
    Start-Process -FilePath "npm.cmd" -ArgumentList @("run","dev") -WorkingDirectory $clientDir -NoNewWindow
    Write-Host "  Client started." -ForegroundColor Green
}

switch ($Action) {
    "status" {
        Show-Status
    }
    "stop" {
        Write-Host ""
        Write-Host "Stopping ChurchBridge AI..." -ForegroundColor Cyan
        Stop-Port $ServerPort "API server"
        Stop-Port $ClientPort "Next.js client"
        Write-Host "Done." -ForegroundColor Green
        Write-Host ""
    }
    "start" {
        Write-Host ""
        Write-Host "Starting ChurchBridge AI..." -ForegroundColor Cyan
        Stop-Port $ServerPort "API server"
        Stop-Port $ClientPort "Next.js client"
        Write-Host ""
        Start-Server
        Start-Client
        Start-Sleep -Seconds 4
        Show-Status
        Write-Host "  API:    http://127.0.0.1:$ServerPort" -ForegroundColor White
        Write-Host "  Client: http://127.0.0.1:$ClientPort" -ForegroundColor White
        Write-Host ""
    }
    "restart" {
        & $PSCommandPath stop
        Start-Sleep -Seconds 1
        & $PSCommandPath start
    }
}
