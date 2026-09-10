#!/usr/bin/env bash
# Nuke: dashboard DB, dashboard audio_calls, and SDRTrunk's own event_logs dir.
# Use this before a fresh capture run.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "wiping db/"
rm -f db/sdrtrunk.db db/sdrtrunk.db-journal db/sdrtrunk.db-wal db/sdrtrunk.db-shm

echo "wiping audio_calls/"
rm -rf audio_calls
mkdir -p audio_calls

if [[ "${1:-}" == "--all-logs" ]]; then
  LOGDIR="${HOME}/SDRTrunk/event_logs"
  if [[ -d "$LOGDIR" ]]; then
    echo "wiping SDRTrunk event_logs at $LOGDIR"
    find "$LOGDIR" -type f -name '*_call_events.log' -delete
  fi
fi

echo "done."
