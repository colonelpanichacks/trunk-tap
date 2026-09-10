#!/usr/bin/env python3
"""
Radio autopilot: makes HackRFs plug-and-play with SDRTrunk.

Every run (via LaunchAgent / systemd timer / cron, once a minute):
  1. Count HackRFs on the USB bus (ioreg on macOS, sysfs or lsusb on Linux).
  2. Require the count to be stable across two samples (anti-flap).
  3. Apply config/radio_plan.json if reality differs from config:
       - each HackRF, in uniqueID order, gets the matching plan slot
         (frequency, sample rate, and that slot's gain profile -- or the
         radio's own `overrides` entry when it has one)
       - a playlist channel is enabled when at least one of its frequencies
         falls inside the receive window of a radio that is actually present,
         and disabled when nothing can hear it
  4. If anything changed: restart SDRTrunk so it picks the config up
     (kill first, THEN write, so SDRTrunk can't clobber edits on exit).

Idempotent: if config already matches the plan, does nothing.
Cooldown: at most one SDRTrunk restart per 5 minutes.

Env: SDRTRUNK_HOME (default ~/SDRTrunk), SDRTRUNK_BIN, SDRTD_RADIO_PLAN.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from radio_plan import (PlanError, apply_settings, covers, hackrf_count,  # noqa: E402
                        hackrfs_in, load_plan, slot_for)

HOME = Path.home()
ROOT = Path(__file__).resolve().parents[1]
SDRTRUNK_HOME = Path(os.environ.get("SDRTRUNK_HOME", HOME / "SDRTrunk"))
TUNER_CFG = SDRTRUNK_HOME / "configuration" / "tuner_configuration.json"
PLAYLIST = SDRTRUNK_HOME / "playlist" / "default.xml"
STATE = ROOT / "logs" / "radio-autopilot-state.json"
LOG = ROOT / "logs" / "radio-autopilot.log"
SDRTRUNK_BIN = os.environ.get(
    "SDRTRUNK_BIN", "/Applications/sdr-trunk/bin/sdr-trunk")

COOLDOWN_SEC = 300


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def stable_count():
    a = hackrf_count()
    time.sleep(10)
    b = hackrf_count()
    if a != b:
        log(f"USB count flapping ({a} -> {b}); skipping this round")
        return None
    return a


def sdrtrunk_running():
    r = subprocess.run(["pgrep", "-f", "io.github.dsheirer.gui.SDRTrunk"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def kill_sdrtrunk():
    subprocess.run(["sudo", "-n", "pkill", "-f",
                    "io.github.dsheirer.gui.SDRTrunk"], capture_output=True)
    for _ in range(20):
        if not sdrtrunk_running():
            return
        time.sleep(1)


def launch_sdrtrunk():
    subprocess.Popen(
        ["sudo", "-n", f"SDR_TRUNK_OPTS=-Duser.home={HOME}", str(SDRTRUNK_BIN)],
        stdout=open("/tmp/sdrtrunk-launch.log", "wb"),
        stderr=subprocess.STDOUT, start_new_session=True)


def channel_freqs(ch):
    """All frequencies (Hz) of a playlist channel, attribute- or child-based."""
    freqs = []
    src = ch.find("source_configuration")
    if src is not None:
        if src.get("frequency"):
            freqs.append(int(src.get("frequency")))
        for f in src.iter("frequency"):
            if f.text:
                freqs.append(int(f.text))
    return freqs


def plan_diff(plan, n_radios):
    """Return (tuner_changes, playlist_changes, data, tree) -- what needs editing."""
    data = json.loads(TUNER_CFG.read_text())
    hackrfs = hackrfs_in(data)

    tuner_changes = []
    live_windows = []
    for i, t in enumerate(hackrfs):
        settings = slot_for(plan, i, t.get("uniqueID"))
        if settings is None:
            continue
        # Only radios actually on the bus can hear anything, so only their
        # windows decide which playlist channels stay enabled.
        if i < n_radios:
            live_windows.append(settings)
        if any(t.get(k) != v for k, v in settings.items() if k != "band"):
            tuner_changes.append((t, settings))

    tree = ET.parse(PLAYLIST)
    playlist_changes = []
    for ch in tree.getroot().iter("channel"):
        freqs = channel_freqs(ch)
        # Only touch channels inside a band this plan is responsible for --
        # anything else is presumably fed by a tuner we don't manage (an
        # RTL-SDR on VHF, say), and disabling it would silence that receiver.
        if not freqs or not any(covers(w, f) for w in plan["slots"] for f in freqs):
            continue
        want_enabled = any(covers(w, f) for w in live_windows for f in freqs)
        is_enabled = ch.get("enabled", "true") != "false"
        if is_enabled != want_enabled:
            playlist_changes.append((ch, want_enabled))

    return tuner_changes, playlist_changes, data, tree


def apply_plan(tuner_changes, playlist_changes, data, tree):
    for t, settings in tuner_changes:
        apply_settings(t, settings)
        log(f"tuner {t.get('uniqueID')} -> {settings['band']} "
            f"{settings['frequency']/1e6:.1f} MHz amp={settings['amplifierEnabled']} "
            f"{settings['lnagain']}/{settings['vgagain']}")
    for ch, enabled in playlist_changes:
        ch.set("enabled", "true" if enabled else "false")
        log(f"channel {ch.get('name')} enabled={enabled}")

    ts = int(time.time())
    shutil.copy(TUNER_CFG, TUNER_CFG.with_suffix(f".json.autopilot.{ts}.backup"))
    shutil.copy(PLAYLIST, PLAYLIST.with_suffix(f".xml.autopilot.{ts}.backup"))
    TUNER_CFG.write_text(json.dumps(data, indent=2))
    tree.write(PLAYLIST, xml_declaration=False, encoding="utf-8")


def main():
    plan = load_plan()

    n = stable_count()
    if n is None or n == 0:
        return

    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass

    tuner_changes, playlist_changes, data, tree = plan_diff(plan, n)
    if not tuner_changes and not playlist_changes:
        return

    last_restart = state.get("last_restart", 0)
    if time.time() - last_restart < COOLDOWN_SEC:
        log(f"plan mismatch but cooling down ({int(time.time()-last_restart)}s ago)")
        return

    log(f"{n} HackRF(s) detected; applying plan "
        f"({len(tuner_changes)} tuner, {len(playlist_changes)} channel changes)")
    was_running = sdrtrunk_running()
    if was_running:
        # Kill FIRST so SDRTrunk can't overwrite our edits on exit.
        kill_sdrtrunk()
    apply_plan(tuner_changes, playlist_changes, data, tree)
    if was_running:
        launch_sdrtrunk()
        log("SDRTrunk relaunched with new plan")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last_restart": time.time(), "radios": n}))


if __name__ == "__main__":
    try:
        main()
    except PlanError as e:
        log(str(e))
        sys.exit(1)
