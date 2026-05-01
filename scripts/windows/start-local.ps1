param(
    [string]$Workspace = "$HOME\.nerya",
    [int]$ApiPort = 18317,
    [int]$DashboardPort = 3001,
    [switch]$OpenDashboard,
    [switch]$ApiOnly,
    [switch]$NoTelegramPoller
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$dashboardDir = Join-Path $repoRoot "dashboard"
$telegramPollerScript = Join-Path $PSScriptRoot "telegram-poller.ps1"
$logDir = Join-Path $Workspace "logs"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-PortListening {
    param([int]$Port)
    $client = New-Object Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(250)) {
            return $false
        }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Start-Api {
    if (Test-PortListening -Port $ApiPort) {
        Write-Host "API already listening on :$ApiPort"
        return
    }

    $command = @(
        "`$env:PYTHONPATH='$repoRoot'"
        "`$env:NERYA_WORKSPACE='$Workspace'"
        "python -m nerya.cli.app run --workspace '$Workspace' --host 127.0.0.1 --port $ApiPort"
    ) -join "; "

    $stdout = Join-Path $logDir "api.out.log"
    $stderr = Join-Path $logDir "api.err.log"
    $process = Start-Process -FilePath "pwsh" `
        -ArgumentList @("-NoProfile", "-Command", $command) `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    Write-Host "Started API pid=$($process.Id) on :$ApiPort"
}

function Start-Dashboard {
    if (Test-PortListening -Port $DashboardPort) {
        Write-Host "Dashboard already listening on :$DashboardPort"
        return
    }

    $command = @(
        "`$env:NERYA_API='http://127.0.0.1:$ApiPort'"
        "`$env:NERYA_BASE_URL='http://127.0.0.1:$ApiPort'"
        "npm run dev"
    ) -join "; "

    $stdout = Join-Path $logDir "dashboard.out.log"
    $stderr = Join-Path $logDir "dashboard.err.log"
    $process = Start-Process -FilePath "pwsh" `
        -ArgumentList @("-NoProfile", "-Command", $command) `
        -WorkingDirectory $dashboardDir `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    Write-Host "Started dashboard pid=$($process.Id) on :$DashboardPort"
}

function Start-TelegramPoller {
    if (-not (Test-Path $telegramPollerScript)) {
        Write-Host "Telegram poller script missing: $telegramPollerScript"
        return
    }

    $escapedScript = $telegramPollerScript.Replace("'", "''")
    $apiUrl = "http://127.0.0.1:$ApiPort"
    $existing = Get-CimInstance Win32_Process -Filter "Name = 'pwsh.exe' OR Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -like "*$escapedScript*" -and $_.CommandLine -like "*$apiUrl*" } |
        Select-Object -First 1
    if ($existing) {
        Write-Host "Telegram poller already running pid=$($existing.ProcessId)"
        return
    }

    $stdout = Join-Path $logDir "telegram-poller.out.log"
    $stderr = Join-Path $logDir "telegram-poller.err.log"
    $process = Start-Process -FilePath "pwsh" `
        -ArgumentList @("-NoProfile", "-File", $telegramPollerScript, "-ApiUrl", $apiUrl) `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    Write-Host "Started Telegram poller pid=$($process.Id)"
}

Start-Api
if (-not $NoTelegramPoller) {
    Start-TelegramPoller
}
if (-not $ApiOnly) {
    Start-Dashboard
}

if ($OpenDashboard -and -not $ApiOnly) {
    Start-Process "http://127.0.0.1:$DashboardPort/dashboard" | Out-Null
}
