param(
    [string]$ShortcutName = "NeryaLocal.cmd",
    [string]$Workspace = "$HOME\.nerya",
    [int]$ApiPort = 18317,
    [int]$DashboardPort = 18380,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$startScript = Join-Path $PSScriptRoot "start-local.ps1"
if (-not (Test-Path -LiteralPath $startScript)) {
    throw "Missing start script: $startScript"
}

$startupDir = [Environment]::GetFolderPath("Startup")
$startupCmd = Join-Path $startupDir $ShortcutName

if ($Remove) {
    if (Test-Path -LiteralPath $startupCmd) {
        Remove-Item -LiteralPath $startupCmd -Force
    }
    Write-Host "Removed autostart entry '$startupCmd'"
    exit 0
}

$command = @(
    "@echo off"
    "pwsh -NoProfile -ExecutionPolicy Bypass -File `"$startScript`" -Workspace `"$Workspace`" -ApiPort $ApiPort -DashboardPort $DashboardPort"
) -join "`r`n"

Set-Content -LiteralPath $startupCmd -Value $command -Encoding ASCII
Write-Host "Installed autostart entry '$startupCmd'"
