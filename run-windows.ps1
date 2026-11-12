<#
 Run the PYTHON implementation natively on Windows (no Docker).
   .\run-windows.ps1                 # real PLC, config\devices.yaml
   .\run-windows.ps1 -Mock           # simulated data, no PLC
   .\run-windows.ps1 -Config devices.sim.yaml
 Needs Python 3.11+ (3.12 recommended) from python.org. Dashboard: http://localhost:8001
#>
param([switch]$Mock, [switch]$Probe, [string]$Config = "devices.yaml", [int]$Port = 8001)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = $null
foreach ($c in @("py -3.12", "py -3.11", "py -3", "python")) {
    $parts = $c.Split(" ")
    try { & $parts[0] $parts[1..9] --version *> $null; if ($LASTEXITCODE -eq 0) { $py = $parts; break } } catch {}
}
if (-not $py) { throw "Python 3.11+ not found. Install from https://www.python.org/downloads/ (tick 'Add to PATH')." }

if (-not (Test-Path .venv)) { & $py[0] $py[1..9] -m venv .venv }
$vpy = ".venv\Scripts\python.exe"
if (-not (Test-Path ".venv\.installed")) {
    & $vpy -m pip install --upgrade pip
    & $vpy -m pip install -r python\requirements.txt
    New-Item .venv\.installed -ItemType File | Out-Null
}

$env:CONFIG_PATH = Join-Path $PSScriptRoot "config\$Config"
$env:DASHBOARD_DIR = Join-Path $PSScriptRoot "dashboard"
$env:S7POC_MOCK = if ($Mock) { "true" } else { "false" }
if ($Probe) { & $vpy python\tools\s7_probe.py; exit $LASTEXITCODE }
Write-Host "Config: $env:CONFIG_PATH  mock=$env:S7POC_MOCK  -> http://localhost:$Port"
& $vpy -m uvicorn app.main:app --app-dir python --host 127.0.0.1 --port $Port
