$ErrorActionPreference = 'Stop'
$publicDemoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$publicDemoStatePath = Join-Path $publicDemoRoot 'tmp\public-demo\state.json'
if (-not (Test-Path -LiteralPath $publicDemoStatePath)) {
    throw 'No public demo state file exists.'
}
$publicDemoState = Get-Content -LiteralPath $publicDemoStatePath -Raw | ConvertFrom-Json
$publicDemoTargets = @()
foreach ($publicDemoKind in @('server','agent')) {
    $publicDemoPid = [int]$publicDemoState.($publicDemoKind + '_pid')
    if ($publicDemoPid -le 0) { throw 'Invalid process ID in public demo state.' }
    $publicDemoProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$publicDemoPid"
    if (-not $publicDemoProcess) { continue }
    if ($publicDemoKind -eq 'server') {
        $publicDemoExpected = $publicDemoProcess.CommandLine -like '*scripts/run_public_demo.py*' `
            -and $publicDemoProcess.CommandLine -match ('--port\s+' + $publicDemoState.port + '\b')
    } else {
        $publicDemoExpected = $publicDemoProcess.Name -eq 'ngrok.exe' `
            -and $publicDemoProcess.CommandLine.Contains("http://127.0.0.1:$($publicDemoState.port)") `
            -and $publicDemoProcess.CommandLine.Contains('--inspect=false') `
            -and $publicDemoProcess.CommandLine.Contains($publicDemoRoot)
    }
    if (-not $publicDemoExpected) {
        throw "Process $publicDemoPid no longer matches this public demo; no processes were stopped."
    }
    $publicDemoTargets += $publicDemoProcess
}
foreach ($publicDemoProcess in $publicDemoTargets) {
    Stop-Process -Id $publicDemoProcess.ProcessId -ErrorAction SilentlyContinue
}
Write-Output 'Public demo processes stopped. Local demo and databases were preserved.'
