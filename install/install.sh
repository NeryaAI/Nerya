#!/usr/bin/env bash
# Nerya one-liner installer (macOS / Linux).
#
# Usage:
#   curl -LsSf https://example.com/install.sh | sh
#   NERYA_HOME=$HOME/.nerya NERYA_SERVICE=1 sh install.sh
#
# Steps:
#   1. Ensure `uv` is installed (https://github.com/astral-sh/uv).
#   2. Create $NERYA_HOME (default: ~/.nerya) and install nerya there.
#   3. Initialise a workspace under $NERYA_WORKSPACE (default: ~/nerya-ws).
#   4. Optionally register a systemd user service / launchd agent that
#      boots the local API server at login.
#
# The script is idempotent; re-run to upgrade or self-heal.

set -euo pipefail

NERYA_HOME="${NERYA_HOME:-$HOME/.nerya}"
NERYA_WORKSPACE="${NERYA_WORKSPACE:-$HOME/nerya-ws}"
NERYA_REF="${NERYA_REF:-main}"
NERYA_SERVICE="${NERYA_SERVICE:-1}"
NERYA_PORT="${NERYA_PORT:-18317}"
# Auto-launch `nerya setup --quick --tui` once everything is in place
# so the operator only has to paste an API key. Set NERYA_NO_AUTO_SETUP=1
# to opt out (CI / headless installs).
NERYA_NO_AUTO_SETUP="${NERYA_NO_AUTO_SETUP:-0}"
# Optional: re-use a local source checkout instead of cloning the
# GitHub mirror. Useful for offline / air-gapped / dev installs.
# Must point to a directory that contains pyproject.toml at its root.
NERYA_SRC="${NERYA_SRC:-}"

say()   { printf '\033[1;36m[nerya]\033[0m %s\n' "$*"; }
note()  { printf '\033[1;90m        %s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }
ok()    { printf '\033[1;32m[ok   ]\033[0m %s\n' "$*"; }
hr()    { printf '\033[1;34m─%.0s\033[0m' {1..60}; printf '\n'; }

require_cmd() {
  local cmd="$1"; shift
  local hint="$*"
  if command -v "$cmd" >/dev/null 2>&1; then return 0; fi
  warn "missing: $cmd"
  if [ -n "$hint" ]; then note "$hint"; fi
  return 1
}

# -------------------------------------------------------------- 0. preflight
preflight() {
  # The script needs `curl`, `git`, and a POSIX shell. uv is installed
  # by us when missing. Surface clear hints instead of failing deep
  # inside a subprocess.
  local fail=0
  require_cmd curl "macOS: curl is preinstalled. Linux: 'sudo apt install curl' or equivalent." || fail=1
  require_cmd git  "macOS: install Xcode CLT (xcode-select --install). Linux: 'sudo apt install git'." || fail=1
  if [ "$fail" = "1" ]; then
    die "missing prerequisites — install the tools above and re-run."
  fi

  # macOS Gatekeeper: warn if the script appears to be under quarantine
  # (best-effort detection — `xattr` may not exist on some hosts).
  if [ "$(uname -s)" = "Darwin" ] && command -v xattr >/dev/null 2>&1; then
    if xattr -p com.apple.quarantine "$0" >/dev/null 2>&1; then
      warn "this script is quarantined by macOS Gatekeeper."
      note "fix with: xattr -d com.apple.quarantine $0"
    fi
  fi
}

# -------------------------------------------------------------- 1. uv
ensure_uv() {
  if command -v uv >/dev/null 2>&1; then ok "uv already installed"; return 0; fi
  say "installing uv (https://github.com/astral-sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv install failed"
  ok "uv ready"
}

# -------------------------------------------------------------- 2. install
install_nerya() {
  mkdir -p "$NERYA_HOME"
  local src_dir
  if [ -n "$NERYA_SRC" ]; then
    # Air-gapped / dev path: re-use an existing checkout. We never copy
    # or symlink — the shim points directly at the user's path so any
    # local edits are picked up instantly.
    if [ ! -f "$NERYA_SRC/pyproject.toml" ]; then
      die "NERYA_SRC=$NERYA_SRC does not contain pyproject.toml"
    fi
    src_dir="$(cd "$NERYA_SRC" && pwd)"
    ok "using local source at $src_dir (NERYA_SRC)"
  else
    src_dir="$NERYA_HOME/src"
    if [ ! -d "$src_dir" ]; then
      say "cloning nerya source into $src_dir"
      git clone --depth 1 --branch "$NERYA_REF" \
        https://github.com/nerya-project/nerya.git "$src_dir" \
        || die "clone failed — set NERYA_SRC=<local path> to install from a local checkout"
    else
      say "updating existing nerya source"
      (cd "$src_dir" && git fetch --depth 1 origin "$NERYA_REF" && git reset --hard FETCH_HEAD) \
        || warn "git update skipped (offline?)"
    fi
  fi
  say "syncing python env with uv (this can take ~30s on first install)"
  (cd "$src_dir" && uv sync --extra trading)
  say "installing CLI shim to $HOME/.local/bin/nerya"
  mkdir -p "$HOME/.local/bin"
  cat > "$HOME/.local/bin/nerya" <<EOF
#!/usr/bin/env bash
exec uv --project "$src_dir" run nerya "\$@"
EOF
  chmod +x "$HOME/.local/bin/nerya"
  # Remember the resolved source directory so the summary + later
  # stages can show / use it.
  NERYA_RESOLVED_SRC="$src_dir"
}

# -------------------------------------------------------------- 3. workspace
ensure_workspace() {
  if [ -d "$NERYA_WORKSPACE/nerya.yml" ] || [ -f "$NERYA_WORKSPACE/nerya.yml" ]; then
    say "workspace already at $NERYA_WORKSPACE"
    return 0
  fi
  mkdir -p "$NERYA_WORKSPACE"
  say "initialising workspace at $NERYA_WORKSPACE"
  "$HOME/.local/bin/nerya" init "$NERYA_WORKSPACE" || \
    warn "nerya init returned non-zero — you can re-run later"
}

# -------------------------------------------------------------- 4. service
install_systemd_user_unit() {
  local unit_dir="$HOME/.config/systemd/user"
  local unit="$unit_dir/nerya.service"
  mkdir -p "$unit_dir"
  cat > "$unit" <<EOF
[Unit]
Description=Nerya Autonomous Agent Runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=NERYA_WORKSPACE=$NERYA_WORKSPACE
Environment=NERYA_PORT=$NERYA_PORT
ExecStart=$HOME/.local/bin/nerya serve --port $NERYA_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now nerya.service
  say "systemd user service installed: systemctl --user status nerya"
}

install_launchd_agent() {
  local plist="$HOME/Library/LaunchAgents/com.nerya.agent.plist"
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.nerya.agent</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/.local/bin/nerya</string>
    <string>serve</string>
    <string>--port</string>
    <string>$NERYA_PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NERYA_WORKSPACE</key><string>$NERYA_WORKSPACE</string>
  </dict>
  <key>StandardOutPath</key><string>$NERYA_HOME/nerya.out.log</string>
  <key>StandardErrorPath</key><string>$NERYA_HOME/nerya.err.log</string>
</dict>
</plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  say "launchd agent installed: $plist"
}

install_service() {
  [ "$NERYA_SERVICE" = "1" ] || { say "skipping service install (NERYA_SERVICE=0)"; return 0; }
  case "$(uname -s)" in
    Linux)   install_systemd_user_unit ;;
    Darwin)  install_launchd_agent ;;
    *)       warn "no service integration for $(uname -s)" ;;
  esac
}

print_summary() {
  hr
  printf '  \033[1;36mNerya is installed.\033[0m\n'
  hr
  printf '  Workspace : %s\n' "$NERYA_WORKSPACE"
  printf '  Source    : %s\n' "${NERYA_RESOLVED_SRC:-$NERYA_HOME/src}"
  printf '  CLI       : %s\n' "$HOME/.local/bin/nerya"
  printf '  API port  : %s\n' "$NERYA_PORT"
  if [ "$NERYA_SERVICE" = "1" ]; then
    printf '  Service   : enabled (boots with login)\n'
  else
    printf '  Service   : disabled (start manually with `nerya serve`)\n'
  fi
  hr
  printf '  \033[1mNext:\033[0m\n'
  printf '    nerya quickstart   # one-command: workspace + service + 1-question setup + open dashboard\n'
  printf '    nerya setup --tui  # 7-step terminal wizard (advanced)\n'
  printf '    nerya setup --quick  # one-question LLM-only setup\n'
  printf '    nerya doctor       # diagnostics\n'
  if [ "$NERYA_SERVICE" = "1" ]; then
    printf '\n  \033[1mService control:\033[0m\n'
    case "$(uname -s)" in
      Linux)
        printf '    systemctl --user status  nerya         # health\n'
        printf '    systemctl --user restart nerya         # bounce after config change\n'
        printf '    journalctl --user -u nerya -f          # live logs\n'
        ;;
      Darwin)
        printf '    launchctl list | grep com.nerya.agent   # health\n'
        printf '    launchctl kickstart -k gui/$(id -u)/com.nerya.agent  # bounce\n'
        printf '    tail -f %s/nerya.err.log               # live logs\n' "$NERYA_HOME"
        ;;
      *)
        printf '    (no per-platform commands for $(uname -s))\n'
        ;;
    esac
  fi
  hr
  printf '  Uninstall later: %s/src/install/uninstall.sh   (or pass --purge)\n' "$NERYA_HOME"
  hr
}

# -------------------------------------------------------------- smoke probe
post_install_smoke() {
  # Cheap "did the shim actually work?" check. We don't run the
  # service — we just want a confirmation that `nerya --version`
  # resolves and produces a non-empty response. Network-free.
  local shim="$HOME/.local/bin/nerya"
  if [ ! -x "$shim" ]; then
    warn "shim not executable at $shim — re-run the installer."
    return 1
  fi
  local out
  if ! out=$("$shim" --version 2>&1); then
    warn "smoke check failed: \`nerya --version\` returned non-zero."
    note "$out"
    note "Try: $shim doctor   (or open a new shell and re-run)"
    return 1
  fi
  ok "smoke: $out"
  return 0
}

ensure_path_active() {
  # If `nerya` is not on PATH in the current shell, surface a clear
  # activation hint. The PATH was already injected into the user's
  # shell profile by uv / our shim; the current shell just needs to
  # re-source it (or open a new terminal).
  if command -v nerya >/dev/null 2>&1; then return 0; fi
  warn "\`nerya\` is not on PATH yet for this shell session."
  note "Run: export PATH=\"\$HOME/.local/bin:\$PATH\""
  note "Or open a fresh terminal — PATH is already saved to your shell profile."
}

auto_run_quick_setup() {
  if [ "$NERYA_NO_AUTO_SETUP" = "1" ]; then
    note "skipping auto setup (NERYA_NO_AUTO_SETUP=1)"
    return 0
  fi
  if ! command -v nerya >/dev/null 2>&1; then
    note "skipping auto setup — \`nerya\` not on PATH yet."
    note "Open a new terminal and run: nerya quickstart"
    return 0
  fi
  # Interactive shells only. Pipe / `curl | sh` paths skip this so the
  # one-liner installer never traps in a TTY prompt.
  if [ ! -t 0 ] || [ ! -t 1 ]; then
    note "non-interactive install — skipping auto setup."
    note "Run \`nerya quickstart\` when you're ready."
    return 0
  fi
  say "launching the quick setup wizard (Ctrl-C to skip)…"
  nerya setup --tui --quick || true
}

main() {
  hr
  printf '  \033[1;36mInstalling Nerya\033[0m\n'
  hr
  say "target:  $NERYA_HOME"
  say "workspc: $NERYA_WORKSPACE"
  say "port:    $NERYA_PORT"
  if [ -n "$NERYA_SRC" ]; then say "src:     $NERYA_SRC (local checkout)"; fi
  preflight
  ensure_uv
  install_nerya
  ensure_workspace
  install_service
  local smoke_ok=1
  post_install_smoke || smoke_ok=0
  print_summary
  ensure_path_active
  if [ "$smoke_ok" = "1" ]; then
    auto_run_quick_setup
  else
    warn "skipping auto setup because the smoke check failed."
    note "Open a new shell and run \`nerya doctor\` to diagnose."
  fi
}

main "$@"
