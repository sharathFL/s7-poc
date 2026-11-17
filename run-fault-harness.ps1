<#
 Fault harness: proxy between the apps and the PLC + scenario runner that scores each protocol.
 Needs the venv from run-windows.ps1 (run that once first). Use THREE PowerShell windows:

   1) .\run-fault-harness.ps1 -Proxy [-Plc 192.168.1.2]      # fault proxy (S7 :1102, OPC UA :14840, control :9000)
   2) .\run-windows.ps1 -Config devices.fault.yaml            # Python app through the proxy  (:8001)
      .\run-windows-go.ps1 -Config devices.fault.yaml          # Go/PLC4X app through the proxy (:8002, optional)
   3) .\run-fault-harness.ps1 -Run [-Only blacksink_20s]      # play scenarios, print PASS/FAIL, write reports\*.json

 Manual fault without the runner:  .\run-fault-harness.ps1 -Mode blacksink   (normal|blacksink|reset|refuse|delay)
 Status:                           .\run-fault-harness.ps1 -Status
 Stop the apps that talk to the PLC directly first (the PLC allows few S7 connections).
#>
param([switch]$Proxy, [switch]$Run, [switch]$Status, [string]$Mode = "", [int]$DelayMs = 0,
      [string]$Plc = "192.168.1.2", [string[]]$Only = @(), [string]$Scenarios = "fault_scenarios.yaml")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$vpy = ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { throw "Run .\run-windows.ps1 once first (creates .venv with PyYAML)." }

if ($Proxy)  { & $vpy python\tools\fault_proxy.py --plc $Plc; exit $LASTEXITCODE }
if ($Status) { Invoke-RestMethod http://127.0.0.1:9000/status | ConvertTo-Json -Depth 5; exit 0 }
if ($Mode)   { Invoke-RestMethod -Method Post "http://127.0.0.1:9000/mode?mode=$Mode&delay_ms=$DelayMs" | ConvertTo-Json -Depth 5; exit 0 }
if ($Run) {
    $a = @("python\tools\fault_scenarios.py", "--scenarios", "config\$Scenarios")
    foreach ($o in $Only) { $a += @("--only", $o) }
    & $vpy @a
    exit $LASTEXITCODE
}
Get-Help $PSCommandPath | Out-Host
