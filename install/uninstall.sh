#!/usr/bin/env bash
# Nerya uninstaller for macOS / Linux.
#
# Mirrors install.sh: stops the systemd/launchd service, removes the
# CLI shim and the source clone under $NERYA_HOME. By default the
# workspace ($NERYA_WORKSPACE) is preserved — data is sacred.
#
# Usage:
#   curl -LsSf https://example.com/uninstall.sh | sh
#   # or, with options:
#   sh uninstall.sh --purge            # also wipe $NERYA_WORKSPACE and $NERYA_HOME
#   sh uninstall.sh --keep-shim        # keep ~/.local/bin/nerya in place
#   sh uninstall.sh --yes              # non-interactive: skip the confirm prompt
#
# Environment overrides:
#   NERYA_HOME       (default: $HOME/.nerya)
#   NERYA_WORKSPACE  (default: $HOME/nerya-ws)
#   NERYA_NO_PROMPT  set to 1 to skip the interactive confirmation (same as --yes)

set -euo pipefail

NERYA_HOME="${NERYA_HOME:-$HOME/.nerya}"
NERYA_WORKSPACE="${NERYA_WORKSPACE:-$HOME/nerya-ws}"
NERYA_NO_PROMPT="${NERYA_NO_PROMPT:-0}"

PURGE=0
KEEP_SHIM=0
YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --purge)      PURGE=1 ;;
    --keep-shim)  KEEP_SHIM=1 ;;
    --yes|-y)     YES=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      printf '[fatal] unknown flag: %s\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

say()   { printf '\033[1;36m[nerya]\033[0m %s\n' "$*"; }
note()  { printf '\033[1;90m        %s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33m[warn ]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }
ok()    { printf '\033[1;32m[ok   ]\033[0m %s\n' "$*"; }
hr()    { printf '\033[1;34m─%.0s\033[0m' {1..60}; printf '\n'; }

# -------------------------------------------------------------- 0. plan
print_plan() {
  hr
  printf '  \033[1;36mNerya uninstaller\033[0m\n'
  hr
  printf '  About to remove:\n'
  if [ "$KEEP_SHIM" = "0" ]; then
    printf '    • CLI shim   : %s/.local/bin/nerya\n' "$HOME"
  else
    printf '    • CLI shim   : (kept — --keep-shim)\n'
  fi
  case "$(uname -s)" in
    Linux)   printf '    • Service    : systemd user unit nerya.service\n' ;;
    Darwin)  printf '    • Service    : launchd agent com.nerya.agent\n' ;;
    *)       printf '    • Service    : (no handler for %s)\n' "$(uname -s)" ;;
  esac
  printf '    • Source     : %s/src\n' "$NERYA_HOME"
  if [ "$PURGE" = "1" ]; then
    printf '    • Workspace  : %s  (--purge)\n' "$NERYA_WORKSPACE"
    printf '    • Nerya home : %s   (--purge)\n' "$NERYA_HOME"
  else
    printf '    • Workspace  : %s  (KEPT — pass --purge to also remove)\n' "$NERYA_WORKSPACE"
    printf '    • Nerya home : %s   (KEPT — pass --purge to also remove)\n' "$NERYA_HOME"
  fi
  hr
}

confirm() {
  [ "$YES" = "1" ] && return 0
  [ "$NERYA_NO_PROMPT" = "1" ] && return 0
  if [ ! -t 0 ] || [ ! -t 1 ]; then
    # Non-interactive pipe ─ refuse to proceed to avoid the "curl|bash
    # accidentally nukes my workspace" failure mode.
    die "non-interactive run — pass --yes (or set NERYA_NO_PROMPT=1) to confirm."
  fi
  printf 'Proceed? [y/N] '
  read -r answer
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *)            die "aborted by user." ;;
  esac
}

# -------------------------------------------------------------- 1. service
remove_service() {
  case "$(uname -s)" in
    Linux)
      if command -v systemctl >/dev/null 2>&1; then
        say "stopping + disabling systemd user unit"
        systemctl --user disable --now nerya.service 2>/dev/null || true
        local unit="$HOME/.config/systemd/user/nerya.service"
        [ -f "$unit" ] && { rm -f "$unit"; ok "removed $unit"; }
        systemctl --user daemon-reload 2>/dev/null || true
      fi
      ;;
    Darwin)
      local plist="$HOME/Library/LaunchAgents/com.nerya.agent.plist"
      if [ -f "$plist" ]; then
        say "unloading launchd agent"
        launchctl unload "$plist" 2>/dev/null || true
        rm -f "$plist"
        ok "removed $plist"
      fi
      ;;
    *)
      note "no service handler for $(uname -s) — skipping."
      ;;
  esac
}

# -------------------------------------------------------------- 2. shim
remove_shim() {
  if [ "$KEEP_SHIM" = "1" ]; then
    note "keeping CLI shim (--keep-shim)"
    return 0
  fi
  local shim="$HOME/.local/bin/nerya"
  if [ -e "$shim" ] || [ -L "$shim" ]; then
    rm -f "$shim"
    ok "removed $shim"
  else
    note "shim already absent at $shim"
  fi
}

# -------------------------------------------------------------- 3. source
remove_source() {
  local src="$NERYA_HOME/src"
  if [ -d "$src" ]; then
    rm -rf "$src"
    ok "removed $src"
  else
    note "source already absent at $src"
  fi
}

# -------------------------------------------------------------- 4. purge
purge_data() {
  [ "$PURGE" = "1" ] || return 0
  if [ -d "$NERYA_WORKSPACE" ]; then
    rm -rf "$NERYA_WORKSPACE"
    ok "removed workspace $NERYA_WORKSPACE"
  fi
  if [ -d "$NERYA_HOME" ]; then
    rm -rf "$NERYA_HOME"
    ok "removed nerya home $NERYA_HOME"
  fi
}

# -------------------------------------------------------------- 5. summary
print_summary() {
  hr
  printf '  \033[1;36mNerya uninstalled.\033[0m\n'
  hr
  if [ "$PURGE" = "0" ]; then
    printf '  Kept (data):\n'
    [ -d "$NERYA_WORKSPACE" ] && printf '    • %s\n' "$NERYA_WORKSPACE"
    [ -d "$NERYA_HOME" ]      && printf '    • %s\n' "$NERYA_HOME"
    printf '  Re-install any time with the one-liner installer; the\n'
    printf '  workspace will be picked up automatically.\n'
  else
    printf '  Purged everything.\n'
  fi
  hr
}

main() {
  print_plan
  confirm
  remove_service
  remove_shim
  remove_source
  purge_data
  print_summary
}

main "$@"
