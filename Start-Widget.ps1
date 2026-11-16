#Requires -Version 7
[CmdletBinding()]
param([string]$ConfigPath=(Join-Path $PSScriptRoot 'config.json'),
      [ValidateSet('Taskbar','TopRight')][string]$Position='Taskbar')
$ErrorActionPreference='Stop'
if (-not $IsWindows) { throw 'The overlay requires Windows. The collector can also run on Linux.' }
$python=(Get-Command python -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw 'Copy config.example.json to config.json and choose your source first.' }
$dataPath=Join-Path $PSScriptRoot 'usage-data.json'
# The launcher owns the collector lifetime. No scheduled task, listener or installed service.
$collector=Start-Process -FilePath $python -ArgumentList @('"'+(Join-Path $PSScriptRoot 'collect.py')+'"','--config','"'+$ConfigPath+'"','--output','"'+$dataPath+'"','--watch') -WindowStyle Hidden -PassThru
try { & (Join-Path $PSScriptRoot 'UsageOverlay.ps1') -DataPath $dataPath -Position $Position }
finally { if (-not $collector.HasExited) { Stop-Process -Id $collector.Id -ErrorAction SilentlyContinue } }
