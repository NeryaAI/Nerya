# Nerya uninstaller for Windows (PowerShell 5.1+).
#
# Mirrors install.ps1: stops the NSSM service, removes the CLI shim,
# wipes the cloned source under $NeryaHome\src. By default the
# workspace ($NeryaWorkspace) is preserved.
#
# Usage:
#   iwr https://example.com/uninstall.ps1 -UseBasicParsing | iex
#   # or, with options:
#   .\uninstall.ps1 -Purge          # also wipe $NeryaWorkspace and $NeryaHome
#   .\uninstall.ps1 -KeepShim       # keep %USERPROFILE%\.local\bin\nerya.cmd
#   .\uninstall.ps1 -Yes            # non-interactive: skip the confirm prompt
#
# Environment overrides:
#   $env:NERYA_HOME       (default: %USERPROFILE%\.nerya)
#   $env:NERYA_WORKSPACE  (default: %USERPROFILE%\nerya-ws)
#   $env:NERYA_NO_PROMPT  set to 1 to skip the interactive confirmation

[CmdletBinding()]
param(
  [switch]$Purge,
  [switch]$KeepShim,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"
function Say     ($m) { Write-Host "[nerya] $m" -ForegroundColor Cyan }
function Note    ($m) { Write-Host "        $m" -ForegroundColor DarkGray }
function Warn    ($m) { Write-Host "[warn ] $m" -ForegroundColor Yellow }
function Fatal   ($m) { Write-Host "[fatal] $m" -ForegroundColor Red; exit 1 }
function Ok      ($m) { Write-Host "[ok   ] $m" -ForegroundColor Green }
function Hr      ()   { Write-Host ("-" * 60) -ForegroundColor Blue }

$NeryaHome      = if ($env:NERYA_HOME)      { $env:NERYA_HOME }      else { Join-Path $env:USERPROFILE ".nerya" }
$NeryaWorkspace = if ($env:NERYA_WORKSPACE) { $env:NERYA_WORKSPACE } else { Join-Path $env:USERPROFILE "nerya-ws" }
$NoPrompt       = if ($env:NERYA_NO_PROMPT) { $env:NERYA_NO_PROMPT } else { "0" }
$Service        = "nerya-agent"
$Shim           = Join-Path $env:USERPROFILE ".local\bin\nerya.cmd"
$Source         = Join-Path $NeryaHome "src"

function Print-Plan {
  Hr
  Write-Host "  Nerya uninstaller" -ForegroundColor Cyan
  Hr
  Write-Host "  About to remove:"
  if (-not $KeepShim) {
    Write-Host ("    - CLI shim   : {0}" -f $Shim)
  } else {
    Write-Host  "    - CLI shim   : (kept -- -KeepShim)"
  }
  Write-Host ("    - Service    : Windows service '{0}' (via NSSM)" -f $Service)
  Write-Host ("    - Source     : {0}" -f $Source)
  if ($Purge) {
    Write-Host ("    - Workspace  : {0}  (-Purge)" -f $NeryaWorkspace)
    Write-Host ("    - Nerya home : {0}  (-Purge)" -f $NeryaHome)
  } else {
    Write-Host ("    - Workspace  : {0}  (KEPT -- pass -Purge to also remove)" -f $NeryaWorkspace)
    Write-Host ("    - Nerya home : {0}  (KEPT -- pass -Purge to also remove)" -f $NeryaHome)
  }
  Hr
}

function Confirm-Removal {
  if ($Yes) { return }
  if ($NoPrompt -eq "1") { return }
  if (-not [Environment]::UserInteractive) {
    Fatal "non-interactive run -- pass -Yes (or set NERYA_NO_PROMPT=1) to confirm."
  }
  $answer = Read-Host "Proceed? [y/N]"
  if ($answer -notmatch '^(y|Y|yes|YES)$') {
    Fatal "aborted by user."
  }
}

function Remove-Service {
  if (Get-Command nssm -ErrorAction SilentlyContinue) {
    Say  "stopping + removing NSSM service '$Service'"
    & nssm stop    $Service 2>$null | Out-Null
    & nssm remove  $Service confirm 2>$null | Out-Null
    Ok   "service '$Service' removed (or absent)"
  } else {
    Note "nssm not on PATH -- skipping service removal"
    Note "If you installed the service via another tool, remove it manually."
  }
}

function Remove-Shim {
  if ($KeepShim) { Note "keeping CLI shim (-KeepShim)"; return }
  if (Test-Path $Shim) {
    Remove-Item -Force $Shim
    Ok "removed $Shim"
  } else {
    Note "shim already absent at $Shim"
  }

  # The shim's bin folder may also be on the user PATH. Surface a hint
  # so the operator can prune it if they want, but never edit Path
  # silently (too easy to nuke unrelated entries).
  $binDir   = Split-Path $Shim
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($userPath -and $userPath -like "*$binDir*") {
    Note "Your user PATH still contains '$binDir'."
    Note "Remove it manually from System Settings -> Environment Variables if no longer needed."
  }
}

function Remove-Source {
  if (Test-Path $Source) {
    Remove-Item -Recurse -Force $Source
    Ok "removed $Source"
  } else {
    Note "source already absent at $Source"
  }
}

function Invoke-Purge {
  if (-not $Purge) { return }
  if (Test-Path $NeryaWorkspace) {
    Remove-Item -Recurse -Force $NeryaWorkspace
    Ok "removed workspace $NeryaWorkspace"
  }
  if (Test-Path $NeryaHome) {
    Remove-Item -Recurse -Force $NeryaHome
    Ok "removed nerya home $NeryaHome"
  }
}

function Print-Summary {
  Hr
  Write-Host "  Nerya uninstalled." -ForegroundColor Cyan
  Hr
  if (-not $Purge) {
    Write-Host "  Kept (data):"
    if (Test-Path $NeryaWorkspace) { Write-Host ("    - {0}" -f $NeryaWorkspace) }
    if (Test-Path $NeryaHome)      { Write-Host ("    - {0}" -f $NeryaHome) }
    Write-Host "  Re-install any time with the one-liner installer;"
    Write-Host "  the workspace will be picked up automatically."
  } else {
    Write-Host "  Purged everything."
  }
  Hr
}

Print-Plan
Confirm-Removal
Remove-Service
Remove-Shim
Remove-Source
Invoke-Purge
Print-Summary
