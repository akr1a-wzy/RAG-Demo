$ErrorActionPreference = 'Stop'
$accessCodeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location -LiteralPath $accessCodeRoot
try {
    & (Join-Path $accessCodeRoot '.venv\Scripts\python.exe') -X utf8 scripts/change_demo_access.py
    if ($LASTEXITCODE -ne 0) { Write-Host 'Change was not fully verified. Keep the report for inspection.' }
} finally {
    Pop-Location
    Read-Host 'Press Enter to close this window'
}
