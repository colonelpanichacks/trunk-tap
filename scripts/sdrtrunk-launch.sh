#!/usr/bin/env bash
# Robust SDRTrunk launcher: kills any stale instance (needs sudoers NOPASSWD from
# install-sudoers.sh), then relaunches with root privileges + user home.
set -euo pipefail
SDRTRUNK="${SDRTRUNK_BIN:-/Applications/sdr-trunk/bin/sdr-trunk}"

if [ ! -x "$SDRTRUNK" ]; then
  echo "[launch] SDRTrunk binary not found at $SDRTRUNK" >&2
  echo "[launch] set SDRTRUNK_BIN=/path/to/sdr-trunk and re-run" >&2
  exit 1
fi

if pgrep -f "io.github.dsheirer.gui.SDRTrunk" >/dev/null 2>&1; then
  echo "[launch] killing existing SDRTrunk"
  sudo -n /usr/bin/pkill -f io.github.dsheirer.gui.SDRTrunk || true
  sleep 2
fi

if ! hackrf_info 2>&1 | grep -q "Found HackRF"; then
  echo "[launch] WARNING: no HackRF detected -- SDRTrunk will start without tuner"
fi

echo "[launch] starting SDRTrunk (sudo -n, HOME=$HOME)"
nohup sudo -n SDR_TRUNK_OPTS="-Duser.home=$HOME" "$SDRTRUNK" \
    >/tmp/sdrtrunk-launch.log 2>&1 &
disown
sleep 6

if pgrep -f "io.github.dsheirer.gui.SDRTrunk" >/dev/null 2>&1; then
  echo "[launch] SDRTrunk up (pid $(pgrep -f io.github.dsheirer.gui.SDRTrunk | head -1))"
  tail -5 /tmp/sdrtrunk-launch.log
else
  echo "[launch] SDRTrunk failed to start -- see /tmp/sdrtrunk-launch.log"
  tail -20 /tmp/sdrtrunk-launch.log
  exit 1
fi
