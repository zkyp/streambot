#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  echo "usage: stream_session.sh ip:port?password=..." >&2
  exit 2
fi

STATE_DIR="$HOME/.config/utstream"
SESSION_LOG="$STATE_DIR/session.log"
TARGET_FILE="$STATE_DIR/target.txt"
PENDING_FILE="$STATE_DIR/pending_target.txt"
WHITELIST="$STATE_DIR/whitelist.txt"
IDLE_UNTIL="$STATE_DIR/idle_until"
LOCKFILE="$STATE_DIR/session.lock"
OBS_ENV="$STATE_DIR/obs.env"
STATE_FILE="$STATE_DIR/state.txt"

UT_LOG="/home/zkyp/.utpg/System/UnrealTournament.log"
OBS_SCENE_TOOL="/home/zkyp/bin/obs_scene.py"

IDLE_TIMEOUT_SECONDS="${IDLE_TIMEOUT_SECONDS:-1800}"
START_GRACE_SECONDS="${START_GRACE_SECONDS:-25}"
END_SWITCH_DELAY_SECONDS="${END_SWITCH_DELAY_SECONDS:-5}"

# Fallback/lobby maps: seeing one of these means the real game is over,
# so switch to the waiting scene. Substring match, so "CTF-Terra" also
# matches "CTF-Terra-LE102" etc.
FALLBACK_MAPS=("CTF-Coret" "CTF-Terra")

mkdir -p "$STATE_DIR"
touch "$WHITELIST"

log() { echo "$(date -Is) $*" >>"$SESSION_LOG"; }
set_state() { printf '%s\n' "$1" >"$STATE_FILE"; }

# Single instance lock
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  log "busy: controller already running"
  echo "busy: controller already running" >&2
  exit 5
fi

# Env / OBS
source /usr/local/bin/xenv.sh
if [[ -f "$OBS_ENV" ]]; then
  set -a
  source "$OBS_ENV"
  set +a
fi

LIVE_SCENE="${OBS_LIVE_SCENE:-UT Live}"
WAIT_SCENE="${OBS_WAIT_SCENE:-Waiting}"

obs_scene_try() {
  local scene="$1"; shift || true
  [[ -x "$OBS_SCENE_TOOL" ]] || return 1
  "$OBS_SCENE_TOOL" "$scene" "$@" >>"$SESSION_LOG" 2>&1
}

obs_scene() {
  obs_scene_try "$@" || true
}

force_scene() {
  # OBS can take a while to expose its websocket on cold start.
  # Wait up to 60s for a successful scene set instead of 10 fast tries.
  local scene="$1"
  local deadline=$(( $(date +%s) + 60 ))
  while (( $(date +%s) < deadline )); do
    if obs_scene_try "$scene"; then
      sleep 0.5
      obs_scene "$scene"
      log "force_scene: scene set to $scene"
      return 0
    fi
    sleep 1
  done
  log "force_scene: OBS websocket unreachable after 60s (scene=$scene)"
  return 0
}

stop_ut_only() {
  systemctl --user stop ut99.service || true
}

stop_obs_and_exit() {
  local why="${1:-}"
  log "stop_obs_and_exit: $why"
  systemctl --user stop ut99.service obs-stream.service || true
  rm -f "$IDLE_UNTIL" 2>/dev/null || true
  set_state "IDLE"
  exit 0
}

wrong_password_wait() {
  log "bad password -> WAITING (keep OBS)"
  LOADMAP_COUNT=0
  enter_waiting
}

rm -f "$IDLE_UNTIL" "$PENDING_FILE" 2>/dev/null || true

printf '%s\n' "$TARGET" >"$TARGET_FILE"
log "start target=$TARGET"

# Start OBS and set live scene
systemctl --user start obs-stream.service
sleep 1
force_scene "$LIVE_SCENE"

# Start UT
systemctl --user start ut99.service
set_state "LIVE"

enter_waiting() {
  local now deadline last_mtime mt left new_target

  now="$(date +%s)"
  deadline="$((now + IDLE_TIMEOUT_SECONDS))"
  printf '%s\n' "$deadline" >"$IDLE_UNTIL"
  last_mtime="$(stat -c %Y "$TARGET_FILE" 2>/dev/null || echo 0)"

  log "enter WAITING (timeout=${IDLE_TIMEOUT_SECONDS}s)"
  set_state "WAITING"

  stop_ut_only
  obs_scene "$WAIT_SCENE" --countdown "$IDLE_TIMEOUT_SECONDS"

  # A target queued while the previous game was still live? Resume into it now.
  if [[ -s "$PENDING_FILE" ]]; then
    new_target="$(cat "$PENDING_FILE" 2>/dev/null | xargs)"
    rm -f "$PENDING_FILE" 2>/dev/null || true
    if [[ -n "$new_target" ]]; then
      log "pending target found -> resuming immediately: $new_target"
      printf '%s\n' "$new_target" >"$TARGET_FILE"
      rm -f "$IDLE_UNTIL" 2>/dev/null || true
      force_scene "$LIVE_SCENE"
      systemctl --user start ut99.service
      set_state "LIVE"
      GRACE_UNTIL="$(( $(date +%s) + START_GRACE_SECONDS ))"
      LOADMAP_COUNT=0
      return 0
    fi
  fi

  while [[ -f "$IDLE_UNTIL" ]]; do
    now="$(date +%s)"
    left="$((deadline - now))"

    if (( left <= 0 )); then
      log "idle timeout reached -> stop OBS + exit"
      systemctl --user stop obs-stream.service || true
      rm -f "$IDLE_UNTIL" 2>/dev/null || true
      set_state "IDLE"
      exit 0
    fi

    obs_scene "$WAIT_SCENE" --countdown "$left"

    mt="$(stat -c %Y "$TARGET_FILE" 2>/dev/null || echo 0)"
    if (( mt > last_mtime )); then
      new_target="$(cat "$TARGET_FILE" 2>/dev/null || true)"
      new_target="$(echo "$new_target" | xargs)"
      last_mtime="$mt"

      if [[ -n "$new_target" ]]; then
        log "resume (target updated): $new_target"
        rm -f "$IDLE_UNTIL" 2>/dev/null || true

        force_scene "$LIVE_SCENE"
        systemctl --user start ut99.service
        set_state "LIVE"

        GRACE_UNTIL="$(( $(date +%s) + START_GRACE_SECONDS ))"
        LOADMAP_COUNT=0
        return 0
      fi
    fi

    sleep 1
  done
}

GRACE_UNTIL="$(( $(date +%s) + START_GRACE_SECONDS ))"
LOADMAP_COUNT=0

log "watcher start (grace=${START_GRACE_SECONDS}s)"

while IFS= read -r line; do
  [[ -f "$IDLE_UNTIL" ]] && continue

  # lowercase copy for case-insensitive matching
  lline="${line,,}"

  # wrong password (case-insensitive, matches NEEDPW variants too)
  if [[ "$lline" == *"failure"* ]]; then
    if [[ "$lline" == *"password"* || "$lline" == *"needpw"* || "$lline" == *"wrongpw"* ]]; then
      wrong_password_wait
      continue
    fi
    # unmatched failure line: log it so we can see the real text if detection still misses
    log "UNMATCHED FAILURE LINE: $line"
  fi

  # detect fallback/lobby maps -> WAITING (scene switch, keep OBS)
  fallback_hit=""
  for fallback_map in "${FALLBACK_MAPS[@]}"; do
    if [[ "$line" == *"$fallback_map"* ]]; then
      fallback_hit="$fallback_map"
      break
    fi
  done
  if [[ -n "$fallback_hit" ]]; then
    log "Fallback map ($fallback_hit) detected -> WAITING in ${END_SWITCH_DELAY_SECONDS}s"
    sleep "$END_SWITCH_DELAY_SECONDS"
    LOADMAP_COUNT=0
    enter_waiting
    continue
  fi

  # ignore join spam during grace window
  if (( $(date +%s) < GRACE_UNTIL )); then
    continue
  fi

  # map end trigger
  if [[ "$line" == *"LoadMap:"* ]]; then
    LOADMAP_COUNT=$((LOADMAP_COUNT + 1))
    if (( LOADMAP_COUNT >= 2 )); then
      log "map end detected -> WAITING in ${END_SWITCH_DELAY_SECONDS}s"
      sleep "$END_SWITCH_DELAY_SECONDS"
      LOADMAP_COUNT=0
      enter_waiting
    fi
    continue
  fi

done < <(tail -n 0 -F "$UT_LOG" 2>>"$SESSION_LOG")
