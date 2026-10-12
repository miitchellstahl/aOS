#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
state="$root/.state"
credentials="$state/credentials"

case "${1:-}" in
  prepare)
    umask 077
    mkdir -p "$credentials"
    for role in bridge display mcp muse; do
      file="$credentials/$role.token"
      if [[ ! -e "$file" ]]; then
        python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$file"
      fi
      chmod 600 "$file"
    done
    printf '%s\n' 'Local credentials ready; values are not printed.'
    exit 0
    ;;
  bridge|mcp|display) ;;
  *)
    printf '%s\n' 'Usage: scripts/dev.sh prepare|bridge|mcp|display' >&2
    exit 2
    ;;
esac

for role in bridge display mcp; do
  if [[ ! -s "$credentials/$role.token" ]]; then
    printf '%s\n' 'Run scripts/dev.sh prepare first.' >&2
    exit 1
  fi
done

export AOS_STATE_DIR="$state"
export AOS_DASHBOARD_PATH="$state/dashboard.json"
export AOS_BRIDGE_URL="http://127.0.0.1:8080"
export AOS_BRIDGE_TOKEN_FILE="$credentials/bridge.token"
export AOS_DISPLAY_TOKEN_FILE="$credentials/display.token"
export AOS_MCP_TOKEN_FILE="$credentials/mcp.token"
export AOS_MUSE_TOKEN_FILE="$credentials/muse.token"

case "$1" in
  bridge) exec uv run --frozen aos-bridge ;;
  mcp) exec uv run --frozen aos-mcp ;;
  display)
    cd app
    exec flutter run -d linux
    ;;
esac
