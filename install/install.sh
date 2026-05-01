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

say() { printf '\033[1;36m[nerya]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }

# -------------------------------------------------------------- 1. uv
ensure_uv() {
  if command -v uv >/dev/null 2>&1; then return 0; fi
  say "installing uv (https://github.com/astral-sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv install failed"
}

# -------------------------------------------------------------- 2. install
install_nerya() {
  mkdir -p "$NERYA_HOME"
  cd "$NERYA_HOME"
  if [ ! -d "src" ]; then
    say "cloning nerya source into $NERYA_HOME/src"
    git clone --depth 1 --branch "$NERYA_REF" \
      https://github.com/nerya-project/nerya.git src \
      || die "clone failed — set NERYA_SRC=<local path> to skip"
  else
    say "updating existing nerya source"
    (cd src && git fetch --depth 1 origin "$NERYA_REF" && git reset --hard FETCH_HEAD) \
      || warn "git update skipped (offline?)"
  fi
  say "syncing python env with uv"
  (cd src && uv sync --extra trading)
  say "installing CLI shim to $HOME/.local/bin/nerya"
  mkdir -p "$HOME/.local/bin"
  cat > "$HOME/.local/bin/nerya" <<EOF
#!/usr/bin/env bash
exec uv --project "$NERYA_HOME/src" run nerya "\$@"
EOF
  chmod +x "$HOME/.local/bin/nerya"
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

main() {
  say "target:  $NERYA_HOME"
  say "workspc: $NERYA_WORKSPACE"
  say "port:    $NERYA_PORT"
  ensure_uv
  install_nerya
  ensure_workspace
  install_service
  cat <<MSG

[nerya] installation complete.

Next steps:
  export PATH="\$HOME/.local/bin:\$PATH"
  nerya dashboard        # open the web UI
  nerya serve --port $NERYA_PORT  # (manual) run the API server

If you enabled the service it is already running on 127.0.0.1:$NERYA_PORT.
MSG
}

main "$@"
