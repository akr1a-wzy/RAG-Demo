param([ValidateRange(1, 65535)][int]$DemoPort = 8000)
$ErrorActionPreference = 'Stop'
$demoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$demoPython = Join-Path $demoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $demoPython)) {
    throw '缺少项目虚拟环境，请按 README 的 uv sync 步骤安装。'
}
$demoListener = Get-NetTCPConnection -LocalPort $DemoPort -State Listen -ErrorAction SilentlyContinue
if ($demoListener) {
    try {
        $demoHealth = Invoke-RestMethod "http://127.0.0.1:$DemoPort/api/health" -TimeoutSec 5
    } catch {
        throw "端口 $DemoPort 已占用且Demo未就绪，请检查该进程或选择其他端口。"
    }
    if ($demoHealth.status -eq 'ready') {
        Write-Host "本机Demo已运行：http://127.0.0.1:$DemoPort/"
        return
    }
    throw 'Demo未就绪，请检查GPU及检索发布状态。'
}
Push-Location -LiteralPath $demoRoot
try {
    & $demoPython -X utf8 scripts/run_demo.py --port $DemoPort
} finally {
    Pop-Location
}
