#Requires -Version 7
[CmdletBinding()]
param([string]$ConfigPath=(Join-Path $PSScriptRoot 'config.json'),
      [ValidateSet('Taskbar','TopRight')][string]$Position='Taskbar')
$ErrorActionPreference='Stop'
if (-not $IsWindows) { throw 'The overlay requires Windows. The collector can also run on Linux.' }
$pythonCommand=Get-Command python,python3,py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$pythonPrefix=@()
if ($pythonCommand) {
    $python=$pythonCommand.Source
    if ($pythonCommand.Name -in 'py','py.exe') { $pythonPrefix=@('-3') }
} else {
    $python=Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Python 3 was not found. Install it and reopen PowerShell.' }
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    $example=Join-Path $PSScriptRoot 'config.example.json'
    if (-not (Test-Path -LiteralPath $example)) { throw 'config.example.json is missing.' }
    Copy-Item -LiteralPath $example -Destination $ConfigPath
    Write-Host "Created $ConfigPath with the local Claude/Codex source."
}
$dataPath=Join-Path $PSScriptRoot 'usage-data.json'
# The launcher owns the collector lifetime. No scheduled task, listener or installed service.
$collectorArgs=@($pythonPrefix)+@('"'+(Join-Path $PSScriptRoot 'collect.py')+'"','--config','"'+$ConfigPath+'"','--output','"'+$dataPath+'"','--watch')
$collector=Start-Process -FilePath $python -ArgumentList $collectorArgs -WindowStyle Hidden -PassThru
try { & (Join-Path $PSScriptRoot 'UsageOverlay.ps1') -DataPath $dataPath -Position $Position }
finally { if (-not $collector.HasExited) { Stop-Process -Id $collector.Id -ErrorAction SilentlyContinue } }
