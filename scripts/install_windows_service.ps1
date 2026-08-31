# Install the Kateb worker as a Windows service.
#
# Uses NSSM (the Non-Sucking Service Manager) to wrap the Python
# worker as a real Windows service. The service will:
#   * start automatically when Windows boots
#   * restart automatically if the worker crashes
#   * log to C:\ProgramData\Kateb\logs\
#
# Prerequisites
# ==============
# 1. Install NSSM:  https://nssm.cc/download
#    (Add C:\Program Files\nssm\ to PATH, or set $Nssm below.)
# 2. Run this script from an *elevated* PowerShell:
#       Right-click → "Run as administrator"
# 3. Make sure the project is in a stable location (this script
#    stores the full path in the service registry).
#
# To remove the service later:
#   nssm stop KatebWorker
#   nssm remove KatebWorker confirm

[CmdletBinding()]
param(
    [string]$PythonExe = (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"),
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$ServiceName = "KatebWorker",
    [string]$Nssm = "nssm"
)

$ErrorActionPreference = "Stop"

# 1) Sanity checks
if (-not (Test-Path $PythonExe)) {
    Write-Host "Python interpreter not found at: $PythonExe" -ForegroundColor Red
    Write-Host "Create a venv first:  python -m venv .venv  then  pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}
if (-not (Get-Command $Nssm -ErrorAction SilentlyContinue)) {
    Write-Host "nssm not found in PATH. Install from https://nssm.cc/download" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Write-Host "No .env found at $ProjectDir\.env" -ForegroundColor Red
    exit 1
}

# 2) Log directory
$logDir = "C:\ProgramData\Kateb\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 3) Worker arguments. NSSM treats PythonExe as the executable and
#    passes the rest as the argument vector. We use the module form
#    so the venv's Python wins.
$workerArgs = @(
    "-m", "scripts.worker",
    "--poll-interval", "5",
    "--chunk-budget", "200"
)

Write-Host "Installing $ServiceName ..." -ForegroundColor Cyan
Write-Host "  project : $ProjectDir"
Write-Host "  python  : $PythonExe"
Write-Host "  args    : $($workerArgs -join ' ')"
Write-Host "  logs    : $logDir"

# 4) Install via NSSM. `nssm install <name> <exe> <args...>` is the
#    one-shot form. After install, we use `nssm set` to tune logging.
$argString = $workerArgs -join ' '
& $Nssm install $ServiceName $PythonExe $argString
& $Nssm set $ServiceName AppDirectory $ProjectDir
& $Nssm set $ServiceName DisplayName "Kateb async processing worker"
& $Nssm set $ServiceName Description "Polls processing_jobs and chunks/embeds documents for the Kateb Arabic writing assistant."
& $Nssm set $ServiceName Start SERVICE_AUTO_START
& $Nssm set $ServiceName AppStdout "$logDir\worker.out.log"
& $Nssm set $ServiceName AppStderr "$logDir\worker.err.log"
& $Nssm set $ServiceName AppRotateFiles 1
& $Nssm set $ServiceName AppRotateBytes 10485760   # 10MB rotation
& $Nssm set $ServiceName AppEnvironmentExtra "PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8"

# 5) Start it
& $Nssm start $ServiceName

Write-Host ""
Write-Host "Service installed and started." -ForegroundColor Green
Write-Host "  status : sc query $ServiceName"
Write-Host "  stop   : nssm stop $ServiceName"
Write-Host "  remove : nssm remove $ServiceName confirm"
Write-Host "  logs   : $logDir\worker.out.log"
