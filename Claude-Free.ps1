# No PowerShell parameter binding: native Claude flags pass through unchanged.
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run .\Setup-Hardened.ps1 first.' }
& $pythonPath (Join-Path $PSScriptRoot 'hardened.py') claude @args
exit $LASTEXITCODE
