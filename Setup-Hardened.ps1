[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$bootstrapUv = Join-Path $PSScriptRoot '.bootstrap\Scripts\uv.exe'
$localUv = Join-Path $PSScriptRoot '.venv\Scripts\uv.exe'
# This script installs pinned tooling and locked packages, not native coding clients.
if (Test-Path -LiteralPath $localUv) { $uvCommand = $localUv }
elseif (Test-Path -LiteralPath $bootstrapUv) { $uvCommand = $bootstrapUv }
else {
    $existingUv = Get-Command uv -ErrorAction Stop
    & $existingUv.Source --no-config venv --python 3.14.0 (Join-Path $PSScriptRoot '.bootstrap')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create bootstrap environment.' }
    & $existingUv.Source --no-config pip install --python (Join-Path $PSScriptRoot '.bootstrap\Scripts\python.exe') 'uv==0.12.13'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install pinned uv.' }
    $uvCommand = $bootstrapUv
}
Push-Location $PSScriptRoot
try {
    & $uvCommand sync --locked --python 3.14.0 --group build --no-install-project --inexact
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
    & $uvCommand pip install --python (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') --no-deps --no-build-isolation --editable $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw 'Local package installation failed.' }
} finally { Pop-Location }
Write-Host 'Ready. Run .\Run-Hardened.ps1. First launch prints the admin temporary password once.'
