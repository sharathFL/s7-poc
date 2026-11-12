<#
 Run the GO implementation (Apache PLC4X plc4go for S7comm) natively on Windows, no Docker.
   .\run-windows-go.ps1                 # real PLC, config\devices.yaml  -> http://localhost:8002
   .\run-windows-go.ps1 -Mock           # simulated data, no PLC
   .\run-windows-go.ps1 -Test           # go test ./... only
 Needs Go 1.27+. If missing, the script offers to install it with winget.
 Stop the Python app first: the S7-1500 allows only a few simultaneous S7 connections.
#>
param([switch]$Mock, [switch]$Test, [string]$Config = "devices.yaml", [int]$Port = 8002)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    $goExe = "C:\Program Files\Go\bin\go.exe"
    if (Test-Path $goExe) { $env:Path += ";C:\Program Files\Go\bin" }
    else {
        Write-Host "Go not found."
        $a = Read-Host "Install it now with winget (GoLang.Go)? [y/N]"
        if ($a -ne "y") { throw "Install Go from https://go.dev/dl/ and re-run." }
        winget install --id GoLang.Go -e --accept-source-agreements --accept-package-agreements
        if (Test-Path $goExe) { $env:Path += ";C:\Program Files\Go\bin" }
        else { throw "Go installed: open a NEW PowerShell window and re-run this script." }
    }
}
go version

Set-Location (Join-Path $PSScriptRoot "go")
if ($Test) { go test ./...; exit $LASTEXITCODE }

$env:CONFIG_PATH = Join-Path $PSScriptRoot "config\$Config"
$env:DASHBOARD_DIR = Join-Path $PSScriptRoot "dashboard"
$env:LISTEN_ADDR = "127.0.0.1:$Port"
$env:S7POC_MOCK = if ($Mock) { "true" } else { "false" }
Write-Host "Config: $env:CONFIG_PATH  mock=$env:S7POC_MOCK  -> http://localhost:$Port"
go run ./cmd/s7poc
