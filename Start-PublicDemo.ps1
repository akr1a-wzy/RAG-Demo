param([ValidateRange(1, 65535)][int]$DemoPort = 8010)
$ErrorActionPreference = 'Stop'
$publicDemoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$publicDemoPython = Join-Path $publicDemoRoot '.venv\Scripts\python.exe'
$publicDemoNgrok = (Get-Command ngrok -ErrorAction Stop).Source
$publicDemoPrivate = Join-Path $publicDemoRoot 'tmp\public-demo'
New-Item -ItemType Directory -Path $publicDemoPrivate -Force | Out-Null
$publicDemoRun = Join-Path $publicDemoPrivate ('run-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $publicDemoRun | Out-Null
& $publicDemoNgrok config check | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Configure the ngrok authtoken locally first.' }
$publicDemoNewProcesses = @()
Push-Location -LiteralPath $publicDemoRoot
try {
    $publicDemoAgent = Get-NetTCPConnection -LocalPort 4040 -State Listen -ErrorAction SilentlyContinue
    if (-not $publicDemoAgent) {
        $publicDemoConnector = Start-Process -FilePath $publicDemoNgrok -ArgumentList @(
            'http', "http://127.0.0.1:$DemoPort", '--inspect=false', '--log-format=json',
            "--log=$publicDemoRun\agent.log"
        ) -WorkingDirectory $publicDemoRoot -WindowStyle Hidden -PassThru `
          -RedirectStandardOutput "$publicDemoRun\agent.stdout.log" `
          -RedirectStandardError "$publicDemoRun\agent.stderr.log"
        $publicDemoNewProcesses += $publicDemoConnector
    }
    $publicDemoTunnel = $null
    for ($publicDemoAttempt = 0; $publicDemoAttempt -lt 25; $publicDemoAttempt++) {
        try {
            $publicDemoTunnels = Invoke-RestMethod 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 2
            $publicDemoTunnel = $publicDemoTunnels.tunnels | Where-Object {
                $_.proto -eq 'https' -and $_.config.addr -eq "http://127.0.0.1:$DemoPort"
            } | Select-Object -First 1
            if ($publicDemoTunnel) { break }
            if ($publicDemoAgent) { throw 'Another ngrok agent uses port 4040; inspect it first.' }
        } catch {
            if ($publicDemoAgent) { throw }
        }
        if ($publicDemoConnector -and $publicDemoConnector.HasExited) { break }
        Start-Sleep -Milliseconds 800
    }
    if (-not $publicDemoTunnel) {
        $publicDemoErrorCode = ''
        $publicDemoErrorPath = Join-Path $publicDemoRun 'agent.stderr.log'
        if (Test-Path -LiteralPath $publicDemoErrorPath) {
            $publicDemoErrorText = Get-Content -LiteralPath $publicDemoErrorPath -Raw
            $publicDemoMatch = [regex]::Match($publicDemoErrorText, 'ERR_NGROK_[0-9]+')
            $publicDemoErrorCode = $publicDemoMatch.Value
        }
        throw "ngrok did not connect. $publicDemoErrorCode Logs: $publicDemoRun"
    }
    $publicDemoOrigin = $publicDemoTunnel.public_url
    & $publicDemoPython -X utf8 scripts/run_public_demo.py prepare --origin $publicDemoOrigin
    if ($LASTEXITCODE -ne 0) { throw 'Public entry configuration failed.' }
    $publicDemoListener = Get-NetTCPConnection -LocalPort $DemoPort -State Listen `
                           -ErrorAction SilentlyContinue
    if (-not $publicDemoListener) {
        $publicDemoServer = Start-Process -FilePath $publicDemoPython -ArgumentList @(
            '-X', 'utf8', 'scripts/run_public_demo.py', 'serve', '--port', "$DemoPort"
        ) -WorkingDirectory $publicDemoRoot -WindowStyle Hidden -PassThru `
          -RedirectStandardOutput "$publicDemoRun\demo.stdout.log" `
          -RedirectStandardError "$publicDemoRun\demo.stderr.log"
        $publicDemoNewProcesses += $publicDemoServer
    }
    $publicDemoConfig = Get-Content -LiteralPath (Join-Path $publicDemoPrivate 'config.json') -Raw |
        ConvertFrom-Json
    $publicDemoAuthRequired = $publicDemoConfig.auth_required -ne $false
    $publicDemoHealthHeaders = @{Host=([Uri]$publicDemoOrigin).Authority}
    if ($publicDemoAuthRequired) {
        $publicDemoAccessLines = Get-Content -LiteralPath (Join-Path $publicDemoPrivate 'access.txt')
        $publicDemoUser = $publicDemoAccessLines[0].Substring('Username: '.Length)
        $publicDemoPass = $publicDemoAccessLines[1].Substring('Password: '.Length)
        $publicDemoBasic = [Convert]::ToBase64String(
            [Text.Encoding]::UTF8.GetBytes($publicDemoUser + ':' + $publicDemoPass))
        $publicDemoHealthHeaders.Authorization = "Basic $publicDemoBasic"
    }
    $publicDemoReady = $false
    for ($publicDemoAttempt = 0; $publicDemoAttempt -lt 45; $publicDemoAttempt++) {
        try {
            $publicDemoHealth = Invoke-RestMethod "http://127.0.0.1:$DemoPort/api/health" `
                -Headers $publicDemoHealthHeaders `
                -TimeoutSec 3
            if ($publicDemoHealth.status -eq 'ready') { $publicDemoReady = $true; break }
        } catch { }
        if ($publicDemoServer -and $publicDemoServer.HasExited) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $publicDemoReady) { throw "Public demo is not ready. Logs: $publicDemoRun" }
    $publicDemoState = [ordered]@{
        origin=$publicDemoOrigin;port=$DemoPort;ready=$true;logs=$publicDemoRun
        auth_required=$publicDemoAuthRequired
        access_file=(Join-Path $publicDemoPrivate 'access.txt')
        server_pid=if ($publicDemoServer) {$publicDemoServer.Id} else {$publicDemoListener.OwningProcess}
        agent_pid=if ($publicDemoConnector) {$publicDemoConnector.Id} else {$publicDemoAgent.OwningProcess}
        session_db='data/public_demo/sessions.sqlite';daily_chat_attempts=50;writes_per_minute=20
        external_validation='pending'
    }
    $publicDemoState | ConvertTo-Json | Set-Content -LiteralPath `
        (Join-Path $publicDemoPrivate 'state.json') -Encoding utf8
    $publicDemoState | ConvertTo-Json
} catch {
    foreach ($publicDemoCreated in $publicDemoNewProcesses) {
        if (-not $publicDemoCreated.HasExited) {
            Stop-Process -Id $publicDemoCreated.Id -ErrorAction SilentlyContinue
        }
    }
    throw
} finally { Pop-Location }
