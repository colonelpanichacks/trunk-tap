#!/usr/bin/env python3
"""
Radio autopilot: makes HackRFs plug-and-play with SDRTrunk.

Every run (via LaunchAgent, once a minute):
  1. Count HackRFs on the USB bus (ioreg).
  2. Require the count to be stable across two samples (anti-flap).
  3. Apply the radio plan if reality differs from config:
       1 radio  -> every tuner config at 772 MHz (700 MHz block);
                   all-800 MHz playlist channels disabled.
       2+ radios -> 1st tuner (by uniqueID) 772 MHz, rest 855 MHz;
                   all-800 MHz playlist channels enabled.
  4. If anything changed: restart SDRTrunk so it picks the config up
     (kill first, THEN write, so SDRTrunk can't clobber edits on exit).

Idempotent: if config already matches the plan, does nothing.
Cooldown: at most one SDRTrunk restart per 5 minutes.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parents[1]
TUNER_CFG = HOME / "SDRTrunk" / "configuration" / "tuner_configuration.json"
PLAYLIST = HOME / "SDRTrunk" / "playlist" / "default.xml"
STATE = ROOT / "logs" / "radio-autopilot-state.json"
LOG = ROOT / "logs" / "radio-autopilot.log"
SDRTRUNK_BIN = os.environ.get(
    "SDRTRUNK_BIN", "/Applications/sdr-trunk/bin/sdr-trunk")

COOLDOWN_SEC = 300
FREQ_700 = 772_000_000
FREQ_800 = 855_000_000


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def hackrf_count():
    out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
                         capture_output=True, text=True).stdout
    return out.count("HackRF One@")


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


def restart_sdrtrunk():
    subprocess.run(["sudo", "-n", "/usr/bin/pkill", "-f",
                    "io.github.dsheirer.gui.SDRTrunk"], capture_output=True)
    for _ in range(20):
        if not sdrtrunk_running():
            break
        time.sleep(1)
    subprocess.Popen(
        ["sudo", "-n", f"SDR_TRUNK_OPTS=-Duser.home={HOME}", str(SDRTRUNK_BIN)],
        stdout=open("/tmp/sdrtrunk-launch.log", "wb"),
        stderr=subprocess.STDOUT, start_new_session=True)
    log("SDRTrunk restarted")


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


def plan_ok(n_radios):
    """Return (ok, tuner_changes, playlist_changes) describing needed edits."""
    tuner_changes = []
    playlist_changes = []

    data = json.loads(TUNER_CFG.read_text())
    hackrfs = sorted((t for t in data.get("tunerConfigurations", [])
                      if t.get("type") == "hackRFTunerConfiguration"),
                     key=lambda t: t.get("uniqueID", ""))
    for i, t in enumerate(hackrfs):
        want = FREQ_700 if (n_radios >= 2 and i == 0) or n_radios < 2 else FREQ_800
        if n_radios >= 2 and i > 0:
            want = FREQ_800
        if t.get("frequency") != want:
            tuner_changes.append((t, want))

    tree = ET.parse(PLAYLIST)
    for ch in tree.getroot().iter("channel"):
        freqs = channel_freqs(ch)
        if freqs and all(f > 800_000_000 for f in freqs):
            want_enabled = n_radios >= 2
            is_enabled = ch.get("enabled", "true") != "false"
            if is_enabled != want_enabled:
                playlist_changes.append((ch, want_enabled))

    return (not tuner_changes and not playlist_changes,
            tuner_changes, playlist_changes, data, tree)


def apply_plan(tuner_changes, playlist_changes, data, tree):
    for t, want in tuner_changes:
        t["frequency"] = want
        t["sampleRate"] = "RATE_10_0"
        t["amplifierEnabled"] = True
        t["lnagain"] = "GAIN_24"
        t["vgagain"] = "GAIN_30"
        t["autoPPMCorrectionEnabled"] = True
        log(f"tuner {t.get('uniqueID')} -> {want/1e6:.1f} MHz")
    for ch, enabled in playlist_changes:
        ch.set("enabled", "true" if enabled else "false")
        log(f"channel {ch.get('name')} enabled={enabled}")

    ts = int(time.time())
    shutil.copy(TUNER_CFG, TUNER_CFG.with_suffix(f".json.autopilot.{ts}.backup"))
    shutil.copy(PLAYLIST, PLAYLIST.with_suffix(f".xml.autopilot.{ts}.backup"))
    TUNER_CFG.write_text(json.dumps(data, indent=2))
    tree.write(PLAYLIST, xml_declaration=False, encoding="utf-8")


def main():
    n = stable_count()
    if n is None or n == 0:
        return

    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass

    ok, tuner_changes, playlist_changes, data, tree = plan_ok(n)
    if ok:
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
        subprocess.run(["sudo", "-n", "/usr/bin/pkill", "-f",
                        "io.github.dsheirer.gui.SDRTrunk"], capture_output=True)
        for _ in range(20):
            if not sdrtrunk_running():
                break
            time.sleep(1)
    apply_plan(tuner_changes, playlist_changes, data, tree)
    if was_running:
        subprocess.Popen(
            ["sudo", "-n", f"SDR_TRUNK_OPTS=-Duser.home={HOME}", str(SDRTRUNK_BIN)],
            stdout=open("/tmp/sdrtrunk-launch.log", "wb"),
            stderr=subprocess.STDOUT, start_new_session=True)
        log("SDRTrunk relaunched with new plan")

    STATE.write_text(json.dumps({"last_restart": time.time(), "radios": n}))


if __name__ == "__main__":
    main()
