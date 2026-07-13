#!/usr/bin/env bash
# NOTE: reconstructed for the repo - the original on the box was never
# captured in version control. Compare against /home/zkyp/bin/stream_stop.sh
# before replacing it. Job: kill the session controller, stop UT + OBS,
# clear session state.
set -uo pipefail

STATE_DIR="$HOME/.config/utstream"

pkill -f /home/zkyp/bin/stream_session.sh 2>/dev/null || true
systemctl --user stop ut99.service obs-stream.service 2>/dev/null || true
rm -f "$STATE_DIR/idle_until" "$STATE_DIR/pending_target.txt" 2>/dev/null || true
printf 'IDLE\n' > "$STATE_DIR/state.txt" 2>/dev/null || true

exit 0
