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

    $telegramEnv = if ($NoTelegramPoller) {
        "`$env:NERYA_DISABLE_TELEGRAM_POLLER='1'"
    } else {
        "Remove-Item Env:NERYA_DISABLE_TELEGRAM_POLLER -ErrorAction SilentlyContinue"
    }
    $command = @(
        "`$env:PYTHONPATH='$repoRoot'"
        "`$env:NERYA_WORKSPACE='$Workspace'"
        $telegramEnv
        "python -m nerya.cli.app run --workspace '$Workspace' --host 127.0.0.1 --port $ApiPort --no-dashboard"
    ) -join "; "

    $stdout = Join-Path $logDir "api.out.log"
    $stderr = Join-Path $logDir "api.err.log"
    $process = Start-Process -FilePath "pwsh" `
        -ArgumentList @("-NoProfile", "-Command", $command) `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
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
        "npm.cmd run dev -- --hostname 127.0.0.1 --port $DashboardPort"
    ) -join "; "

    $stdout = Join-Path $logDir "dashboard.out.log"
    $stderr = Join-Path $logDir "dashboard.err.log"
    $process = Start-Process -FilePath "pwsh" `
        -ArgumentList @("-NoProfile", "-Command", $command) `
        -WorkingDirectory $dashboardDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    Write-Host "Started dashboard pid=$($process.Id) on :$DashboardPort"
}

function Wait-Api {
    param([int]$Attempts = 30)
    $healthUrl = "http://127.0.0.1:$ApiPort/health"
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            Invoke-RestMethod -Method Get -Uri $healthUrl -TimeoutSec 2 | Out-Null
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Show-GatewayStatus {
    if (-not (Wait-Api)) {
        Write-Host "Gateway status skipped: API did not become healthy on :$ApiPort"
        return
    }

    $statusUrl = "http://127.0.0.1:$ApiPort/gateway/status"
    try {
        $status = Invoke-RestMethod -Method Get -Uri $statusUrl -TimeoutSec 5
    } catch {
        Write-Host "Gateway status unavailable at $statusUrl"
        return
    }

    if (-not $status.channels_file_exists) {
        Write-Host "Gateway: no messages/channels.yml configured; Telegram is not started."
        return
    }

    $telegram = $status.telegram
    $channels = @($telegram.channels)
    if ($channels.Count -eq 0) {
        Write-Host "Gateway: channels.yml has no Telegram channel."
        return
    }

    $running = @($channels | Where-Object { $_.poller_alive })
    if ($telegram.polling_disabled_by_env) {
        Write-Host "Gateway: Telegram polling disabled by NERYA_DISABLE_TELEGRAM_POLLER."
    } elseif ($running.Count -gt 0) {
        $names = ($running | ForEach-Object { $_.channel }) -join ", "
        Write-Host "Gateway: Telegram poller running for $names."
    } else {
        Write-Host "Gateway: Telegram configured, but no poller is currently alive."
    }
}

Start-Api
Show-GatewayStatus
if (-not $ApiOnly) {
    Start-Dashboard
}

if ($OpenDashboard -and -not $ApiOnly) {
    Start-Process "http://127.0.0.1:$DashboardPort/dashboard" | Out-Null
}
