#!/usr/bin/env bash
set -euo pipefail

ZKYP_USER="zkyp"
STATE_DIR="/home/zkyp/.config/utstream"
LOCK_FILE="$STATE_DIR/session.lock"
TARGET_FILE="$STATE_DIR/target.txt"
STOP_SCRIPT="/home/zkyp/bin/stream_stop.sh"
BOT_SERVICE="utbot.service"  # system service (not --user)
USER_SERVICES=("obs-stream.service" "ut99.service")

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }

uid="$(id -u "$ZKYP_USER")"

userctl() {
  if [[ "$(id -un)" == "$ZKYP_USER" ]]; then
    systemctl --user "$@"
  else
    sudo -n -u "$ZKYP_USER" \
      XDG_RUNTIME_DIR="/run/user/$uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      systemctl --user "$@"
  fi
}

sysctl() {
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl "$@"
  else
    sudo -n systemctl "$@"
  fi
}

log "RESETALL: stopping active stream (best effort)"
if [[ -x "$STOP_SCRIPT" ]]; then
  set +e
  sudo -n -u "$ZKYP_USER" "$STOP_SCRIPT" >/dev/null 2>&1
  set -e
fi

log "RESETALL: restarting user services: ${USER_SERVICES[*]}"
for svc in "${USER_SERVICES[@]}"; do
  set +e
  userctl restart "$svc" >/dev/null 2>&1
  set -e
done

log "RESETALL: clearing stale state files (best effort)"
set +e
sudo -n -u "$ZKYP_USER" rm -f "$LOCK_FILE" "$TARGET_FILE" >/dev/null 2>&1
set -e

log "RESETALL: restarting bot service: $BOT_SERVICE"
sysctl restart "$BOT_SERVICE"

log "RESETALL: done"
