[CmdletBinding()]
param(
    [Parameter(Position=0)]
    [ValidateSet('serve', 'ensure-server', 'reset-password', 'show-initial-password', 'claude', 'codex', 'route')]
    [string]$Command = 'serve',
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$ClientArguments
)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Run .\Setup-Hardened.ps1 first.'
}
# Keep the caller's current project directory for coding clients.
& $pythonPath (Join-Path $PSScriptRoot 'hardened.py') $Command @ClientArguments
exit $LASTEXITCODE
