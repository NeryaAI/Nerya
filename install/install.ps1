# Nerya one-liner installer for Windows 10/11 (PowerShell 5.1+).
#
# Usage (one-liner):
#   iwr https://example.com/install.ps1 -UseBasicParsing | iex
#
# Optional env vars:
#   $env:NERYA_HOME       default %USERPROFILE%\.nerya
#   $env:NERYA_WORKSPACE  default %USERPROFILE%\nerya-ws
#   $env:NERYA_PORT       default 18317
#   $env:NERYA_SERVICE    0 = skip NSSM service; 1 = install (default)
#
# Steps:
#   1. ensure `uv` (winget or direct installer)
#   2. clone / update Nerya under $NERYA_HOME\src
#   3. `uv sync --extra trading`
#   4. drop a nerya.cmd shim into %USERPROFILE%\.local\bin (added to PATH)
#   5. optionally install an NSSM service so nerya boots with Windows

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
function Say    ($m) { Write-Host "[nerya] $m" -ForegroundColor Cyan }
function Warn   ($m) { Write-Host "[warn ] $m" -ForegroundColor Yellow }
function Fatal  ($m) { Write-Host "[fatal] $m" -ForegroundColor Red; exit 1 }

$NeryaHome      = if ($env:NERYA_HOME)      { $env:NERYA_HOME }      else { Join-Path $env:USERPROFILE ".nerya" }
$NeryaWorkspace = if ($env:NERYA_WORKSPACE) { $env:NERYA_WORKSPACE } else { Join-Path $env:USERPROFILE "nerya-ws" }
$NeryaRef       = if ($env:NERYA_REF)       { $env:NERYA_REF }       else { "main" }
$NeryaPort      = if ($env:NERYA_PORT)      { $env:NERYA_PORT }      else { "18317" }
$NeryaService   = if ($env:NERYA_SERVICE)   { $env:NERYA_SERVICE }   else { "1" }

function Ensure-Uv {
  if (Get-Command uv -ErrorAction SilentlyContinue) { return }
  Say "installing uv"
  try {
    iwr https://astral.sh/uv/install.ps1 -UseBasicParsing | iex
  } catch {
    Fatal "uv install failed: $_"
  }
  $env:PATH = "$env:USERPROFILE\.cargo\bin;$env:USERPROFILE\.local\bin;$env:PATH"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fatal "uv not on PATH" }
}

function Install-Nerya {
  New-Item -ItemType Directory -Force -Path $NeryaHome | Out-Null
  $src = Join-Path $NeryaHome "src"
  if (-not (Test-Path $src)) {
    Say "cloning nerya source into $src"
    git clone --depth 1 --branch $NeryaRef https://github.com/nerya-project/nerya.git $src
  } else {
    Say "updating existing nerya source"
    try {
      git -C $src fetch --depth 1 origin $NeryaRef
      git -C $src reset --hard FETCH_HEAD
    } catch {
      Warn "git update skipped: $_"
    }
  }
  Say "uv sync --extra trading"
  uv --project $src sync --extra trading

  $binDir = Join-Path $env:USERPROFILE ".local\bin"
  New-Item -ItemType Directory -Force -Path $binDir | Out-Null
  $shim = Join-Path $binDir "nerya.cmd"
  @"
@echo off
uv --project "$src" run nerya %*
"@ | Set-Content -Encoding ASCII $shim

  # append %USERPROFILE%\.local\bin to user PATH if not present
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not ($userPath -like "*$binDir*")) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
    Say "added $binDir to user PATH (open a new terminal to pick it up)"
  }
}

function Ensure-Workspace {
  if (Test-Path (Join-Path $NeryaWorkspace "nerya.yml")) {
    Say "workspace already at $NeryaWorkspace"
    return
  }
  Say "initialising workspace at $NeryaWorkspace"
  & (Join-Path $env:USERPROFILE ".local\bin\nerya.cmd") init $NeryaWorkspace
}

function Install-Nssm-Service {
  if ($NeryaService -ne "1") { Say "skipping service install (NERYA_SERVICE=0)"; return }
  if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Warn "nssm not found on PATH — install via 'winget install nssm' or 'choco install nssm' then re-run with NERYA_SERVICE=1"
    return
  }
  $svc = "nerya-agent"
  Say "installing Windows service '$svc' via NSSM"
  nssm stop    $svc    2>$null
  nssm remove  $svc confirm 2>$null
  $exe = Join-Path $env:USERPROFILE ".local\bin\nerya.cmd"
  nssm install $svc $exe "serve" "--port" $NeryaPort
  nssm set     $svc AppEnvironmentExtra NERYA_WORKSPACE=$NeryaWorkspace
  nssm set     $svc Start SERVICE_AUTO_START
  nssm set     $svc AppStdout (Join-Path $NeryaHome "nerya.out.log")
  nssm set     $svc AppStderr (Join-Path $NeryaHome "nerya.err.log")
  nssm start   $svc
  Say "service status: sc query $svc"
}

Say  "target:  $NeryaHome"
Say  "workspc: $NeryaWorkspace"
Say  "port:    $NeryaPort"
Ensure-Uv
Install-Nerya
Ensure-Workspace
Install-Nssm-Service

Write-Host ""
Say "installation complete."
Say "Open a new PowerShell and run:   nerya dashboard"
