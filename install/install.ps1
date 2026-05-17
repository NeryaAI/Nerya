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
function Say     ($m) { Write-Host "[nerya] $m" -ForegroundColor Cyan }
function Note    ($m) { Write-Host "        $m" -ForegroundColor DarkGray }
function Warn    ($m) { Write-Host "[warn ] $m" -ForegroundColor Yellow }
function Fatal   ($m) { Write-Host "[fatal] $m" -ForegroundColor Red; exit 1 }
function Ok      ($m) { Write-Host "[ok   ] $m" -ForegroundColor Green }
function Hr      ()   { Write-Host ("-" * 60) -ForegroundColor Blue }

$NeryaHome         = if ($env:NERYA_HOME)             { $env:NERYA_HOME }         else { Join-Path $env:USERPROFILE ".nerya" }
$NeryaWorkspace    = if ($env:NERYA_WORKSPACE)        { $env:NERYA_WORKSPACE }    else { Join-Path $env:USERPROFILE "nerya-ws" }
$NeryaRef          = if ($env:NERYA_REF)              { $env:NERYA_REF }          else { "main" }
$NeryaPort         = if ($env:NERYA_PORT)             { $env:NERYA_PORT }         else { "18317" }
$NeryaService      = if ($env:NERYA_SERVICE)          { $env:NERYA_SERVICE }      else { "1" }
$NeryaNoAutoSetup  = if ($env:NERYA_NO_AUTO_SETUP)    { $env:NERYA_NO_AUTO_SETUP } else { "0" }
# Optional: re-use a local source checkout instead of cloning the
# GitHub mirror. Useful for offline / air-gapped / dev installs.
$NeryaSrc          = if ($env:NERYA_SRC)              { $env:NERYA_SRC }          else { "" }
$Script:NeryaResolvedSrc = $null

function Ensure-Git {
  if (Get-Command git -ErrorAction SilentlyContinue) { Ok "git already installed"; return }
  Warn "git is not on PATH."
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Note "Trying: winget install --id Git.Git -e --silent"
    try {
      winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
      # winget installs to %ProgramFiles%\Git\cmd by default; surface it
      # to the current session.
      $env:PATH = "$env:ProgramFiles\Git\cmd;$env:PATH"
    } catch {
      Warn "winget install failed: $_"
    }
  }
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Note "Install git manually: https://git-scm.com/download/win"
    Note "Then re-run this installer."
    Fatal "git missing"
  }
  Ok "git ready"
}

function Ensure-Uv {
  if (Get-Command uv -ErrorAction SilentlyContinue) { Ok "uv already installed"; return }
  Say "installing uv"
  try {
    iwr https://astral.sh/uv/install.ps1 -UseBasicParsing | iex
  } catch {
    Fatal "uv install failed: $_"
  }
  $env:PATH = "$env:USERPROFILE\.cargo\bin;$env:USERPROFILE\.local\bin;$env:PATH"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fatal "uv not on PATH" }
  Ok "uv ready"
}

function Install-Nerya {
  New-Item -ItemType Directory -Force -Path $NeryaHome | Out-Null

  if ($NeryaSrc -and $NeryaSrc.Trim() -ne "") {
    $pyproject = Join-Path $NeryaSrc "pyproject.toml"
    if (-not (Test-Path $pyproject)) {
      Fatal "NERYA_SRC=$NeryaSrc does not contain pyproject.toml"
    }
    $src = (Resolve-Path $NeryaSrc).Path
    Ok "using local source at $src (NERYA_SRC)"
  } else {
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
  }

  Say "uv sync --extra trading (this can take ~30s on first install)"
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

  # Surface the resolved src path to later stages (summary, smoke).
  $Script:NeryaResolvedSrc = $src
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

function Print-Summary {
  Hr
  Write-Host "  Nerya is installed." -ForegroundColor Cyan
  Hr
  Write-Host ("  Workspace : {0}" -f $NeryaWorkspace)
  if ($Script:NeryaResolvedSrc) {
    Write-Host ("  Source    : {0}" -f $Script:NeryaResolvedSrc)
  } else {
    Write-Host ("  Source    : {0}" -f (Join-Path $NeryaHome "src"))
  }
  Write-Host ("  CLI       : {0}" -f (Join-Path $env:USERPROFILE ".local\bin\nerya.cmd"))
  Write-Host ("  API port  : {0}" -f $NeryaPort)
  if ($NeryaService -eq "1") {
    Write-Host "  Service   : enabled (boots with Windows via NSSM)"
  } else {
    Write-Host "  Service   : disabled (start manually with `nerya serve`)"
  }
  Hr
  Write-Host "  Next:" -ForegroundColor Yellow
  Write-Host "    nerya quickstart    # one-command: workspace + service + 1-question setup + open dashboard"
  Write-Host "    nerya setup --tui   # 7-step terminal wizard (advanced)"
  Write-Host "    nerya setup --quick # one-question LLM-only setup"
  Write-Host "    nerya doctor        # diagnostics"

  if ($NeryaService -eq "1") {
    Write-Host ""
    Write-Host "  Service control:" -ForegroundColor Yellow
    Write-Host "    sc query nerya-agent             # health"
    Write-Host "    nssm restart nerya-agent         # bounce after config change"
    Write-Host ("    Get-Content -Wait '{0}\nerya.err.log'   # live logs" -f $NeryaHome)
  }

  Hr
  Write-Host ("  Uninstall later: powershell -ExecutionPolicy Bypass -File '{0}\src\install\uninstall.ps1'   (or pass -Purge)" -f $NeryaHome)
  Hr
}

function Post-Install-Smoke {
  # Cheap "did the shim actually work?" check. We don't run the
  # service -- we just want a confirmation that `nerya --version`
  # resolves and produces a non-empty response. Network-free.
  $shim = Join-Path $env:USERPROFILE ".local\bin\nerya.cmd"
  if (-not (Test-Path $shim)) {
    Warn "shim not found at $shim -- re-run the installer."
    return $false
  }
  try {
    $out = & $shim --version 2>&1
    if ($LASTEXITCODE -ne 0) {
      Warn "smoke check failed: ``nerya --version`` returned $LASTEXITCODE"
      Note ($out -join "`n")
      return $false
    }
    Ok "smoke: $out"
    return $true
  } catch {
    Warn "smoke check failed: $_"
    return $false
  }
}

function Auto-Run-Quick-Setup {
  if ($NeryaNoAutoSetup -eq "1") {
    Note "skipping auto setup (NERYA_NO_AUTO_SETUP=1)"
    return
  }
  if (-not (Get-Command nerya -ErrorAction SilentlyContinue)) {
    Note "skipping auto setup — `nerya` not on PATH yet."
    Note "Open a new PowerShell and run: nerya quickstart"
    return
  }
  Say "launching the quick setup wizard (Ctrl-C to skip)..."
  try {
    & nerya setup --tui --quick
  } catch {
    Note "auto setup exited: $_"
  }
}

Hr
Write-Host "  Installing Nerya" -ForegroundColor Cyan
Hr
Say  "target:  $NeryaHome"
Say  "workspc: $NeryaWorkspace"
Say  "port:    $NeryaPort"
if ($NeryaSrc) { Say "src:     $NeryaSrc (local checkout)" }
Ensure-Git
Ensure-Uv
Install-Nerya
Ensure-Workspace
Install-Nssm-Service
$Script:SmokeOk = Post-Install-Smoke
if (-not $Script:SmokeOk) { Warn "skipping auto setup because the smoke check failed." }
Print-Summary
if ($Script:SmokeOk) { Auto-Run-Quick-Setup }
