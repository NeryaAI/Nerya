param(
    [string]$Workspace = "$HOME\.nerya",
    [int]$ApiPort = 18317,
    [int]$DashboardPort = 0,
    [switch]$OpenDashboard,
    [switch]$ApiOnly
)
# Note: the legacy ``-NoTelegramPoller`` switch was removed (2026-05-07).
# It was the most common cause of "configured but no replies" — running
# the script with that switch left ``NERYA_DISABLE_TELEGRAM_POLLER=1`` in
# the spawned pwsh, which silently killed inbound message handling while
# outbound (trade pushes) still worked.  If you really want to disable
# polling, set ``$env:NERYA_DISABLE_TELEGRAM_POLLER='1'`` yourself
# before invoking the script — but the default is now always-on.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$dashboardDir = Join-Path $repoRoot "dashboard"
$logDir = Join-Path $Workspace "logs"
$DefaultDashboardPort = 18380

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Resolve-NeryaConfigInt {
    param(
        [string]$Key,
        [int]$Default
    )
    $oldPythonPath = $env:PYTHONPATH
    $oldWorkspace = $env:NERYA_CONFIG_WORKSPACE
    $oldKey = $env:NERYA_CONFIG_KEY
    $oldDefault = $env:NERYA_CONFIG_DEFAULT
    try {
        $env:PYTHONPATH = $repoRoot
        $env:NERYA_CONFIG_WORKSPACE = $Workspace
        $env:NERYA_CONFIG_KEY = $Key
        $env:NERYA_CONFIG_DEFAULT = [string]$Default
        $value = & python -c "import os; from nerya.core.config import load_config; cfg = load_config(os.environ.get('NERYA_CONFIG_WORKSPACE')); print(cfg.get(os.environ.get('NERYA_CONFIG_KEY'), os.environ.get('NERYA_CONFIG_DEFAULT')))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $value) {
            $parsed = 0
            if ([int]::TryParse(($value | Select-Object -First 1).ToString().Trim(), [ref]$parsed) -and $parsed -gt 0 -and $parsed -le 65535) {
                return $parsed
            }
        }
    } catch {
        return $Default
    } finally {
        if ($null -eq $oldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $oldPythonPath }
        if ($null -eq $oldWorkspace) { Remove-Item Env:NERYA_CONFIG_WORKSPACE -ErrorAction SilentlyContinue } else { $env:NERYA_CONFIG_WORKSPACE = $oldWorkspace }
        if ($null -eq $oldKey) { Remove-Item Env:NERYA_CONFIG_KEY -ErrorAction SilentlyContinue } else { $env:NERYA_CONFIG_KEY = $oldKey }
        if ($null -eq $oldDefault) { Remove-Item Env:NERYA_CONFIG_DEFAULT -ErrorAction SilentlyContinue } else { $env:NERYA_CONFIG_DEFAULT = $oldDefault }
    }
    return $Default
}

if ((-not $PSBoundParameters.ContainsKey("DashboardPort")) -or $DashboardPort -le 0) {
    $DashboardPort = Resolve-NeryaConfigInt -Key "dashboard.port" -Default $DefaultDashboardPort
}

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

function Get-PortListenerPids {
    param([int]$Port)
    @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Stop-PortListeners {
    param([int]$Port)
    $listenerPids = Get-PortListenerPids -Port $Port
    foreach ($listenerPid in $listenerPids) {
        if ($listenerPid -and $listenerPid -ne $PID) {
            Write-Host "Stopping stale listener pid=$listenerPid on :$Port"
            Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue
        }
    }
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Test-PortListening -Port $Port)) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
}

function Clear-DashboardBuildCache {
    $nextDir = Join-Path $dashboardDir ".next"
    if (-not (Test-Path -LiteralPath $nextDir)) {
        return
    }
    $dashboardFull = [System.IO.Path]::GetFullPath($dashboardDir)
    $nextFull = [System.IO.Path]::GetFullPath($nextDir)
    if (-not $nextFull.StartsWith($dashboardFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove dashboard cache outside dashboard: $nextFull"
    }
    Write-Host "Clearing stale dashboard cache at $nextFull"
    Remove-Item -LiteralPath $nextFull -Recurse -Force
}

function Test-DashboardProxy {
    $healthUrl = "http://127.0.0.1:$DashboardPort/api/proxy/health"
    try {
        $health = Invoke-RestMethod -Method Get -Uri $healthUrl -TimeoutSec 5
        return ($health.status -eq "ok")
    } catch {
        return $false
    }
}

function Test-DashboardRootChunk {
    $rootUrl = "http://127.0.0.1:$DashboardPort/"
    $chunkUrl = "http://127.0.0.1:$DashboardPort/_next/static/chunks/app/page.js"
    try {
        $root = Invoke-WebRequest -UseBasicParsing -Uri $rootUrl -TimeoutSec 25
        if ($root.StatusCode -ne 200) {
            return $false
        }
        $chunk = Invoke-WebRequest -UseBasicParsing -Uri $chunkUrl -TimeoutSec 10
        return ($chunk.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Wait-DashboardProxy {
    param([int]$Attempts = 40)
    for ($i = 0; $i -lt $Attempts; $i++) {
        if (Test-DashboardProxy) {
            return $true
        }
        Start-Sleep -Milliseconds 750
    }
    return $false
}

function Start-Api {
    if (Test-PortListening -Port $ApiPort) {
        Write-Host "API already listening on :$ApiPort"
        return
    }

    # Always clear the polling kill-switch — see header note. Operators
    # who truly want it disabled should export the env var themselves.
    $command = @(
        "`$env:PYTHONPATH='$repoRoot'"
        "`$env:NERYA_WORKSPACE='$Workspace'"
        "Remove-Item Env:NERYA_DISABLE_TELEGRAM_POLLER -ErrorAction SilentlyContinue"
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
        if ((Test-DashboardProxy) -and (Test-DashboardRootChunk)) {
            Write-Host "Dashboard already healthy on :$DashboardPort"
            return
        }
        Write-Host "Dashboard listener on :$DashboardPort is unhealthy or points at the wrong API; restarting it."
        Stop-PortListeners -Port $DashboardPort
        Clear-DashboardBuildCache
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
    if (-not (Wait-DashboardProxy)) {
        Write-Host "Dashboard proxy did not become healthy on :$DashboardPort; inspect $stderr"
    }
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
