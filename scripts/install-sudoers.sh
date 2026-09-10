#!/usr/bin/env bash
# One-time setup. Installs a sudoers entry so:
#   sudo -n /path/to/sdr-trunk/bin/sdr-trunk
#   sudo -n pkill -f io.github.dsheirer.gui.SDRTrunk
# work with NO password prompt (and SETENV so HOME can be passed).
#
# Set SDRTRUNK_BIN to your SDRTrunk launcher path (defaults to
# /Applications/sdr-trunk/bin/sdr-trunk).
#
# Requires root: run as `sudo bash install-sudoers.sh`.
# Uses `visudo -c` to validate before writing so a bad edit can't lock you out.

set -euo pipefail

USER_NAME=$(logname 2>/dev/null || true)
if [ -z "${USER_NAME:-}" ]; then
  echo "could not determine the invoking user (logname failed)" >&2
  echo "re-run as: SUDO_USER=<your-username> sudo bash $0" >&2
  exit 1
fi
SDRTRUNK="${SDRTRUNK_BIN:-/Applications/sdr-trunk/bin/sdr-trunk}"
TARGET=/etc/sudoers.d/trunk-tap

if [ "$EUID" -ne 0 ]; then
  echo "please run as: sudo bash $0" >&2
  exit 1
fi

if [ ! -x "$SDRTRUNK" ]; then
  echo "SDRTrunk binary not found at $SDRTRUNK" >&2
  echo "point SDRTRUNK_BIN at your sdr-trunk launcher and re-run, e.g.:" >&2
  echo "  SDRTRUNK_BIN=/path/to/sdr-trunk sudo bash $0" >&2
  exit 1
fi

TMP=$(mktemp)
cat > "$TMP" <<EOF
# trunk-tap: passwordless launch/kill of SDRTrunk (root needed for HackRF USB detach on macOS)
Defaults!$SDRTRUNK setenv
$USER_NAME ALL=(root) NOPASSWD: SETENV: $SDRTRUNK
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/pkill -f io.github.dsheirer.gui.SDRTrunk
$USER_NAME ALL=(root) NOPASSWD: /bin/ls /var/root/SDRTrunk*
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/find /var/root/SDRTrunk*
EOF

# Validate
if ! visudo -c -f "$TMP"; then
  echo "sudoers snippet failed validation, aborting" >&2
  rm -f "$TMP"
  exit 1
fi

install -m 0440 -o root -g wheel "$TMP" "$TARGET"
rm -f "$TMP"
echo "installed: $TARGET"
echo "you can now run without password:"
echo "  sudo -n HOME=\$HOME $SDRTRUNK"
echo "  sudo -n pkill -f io.github.dsheirer.gui.SDRTrunk"
