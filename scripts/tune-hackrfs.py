#!/usr/bin/env python3
"""
Auto-tune every HackRF in SDRTrunk's tuner_configuration.json to the band plan
in config/radio_plan.json.

Radios are matched to plan slots in uniqueID order -- 1st radio gets slot 0,
2nd slot 1, 3rd slot 2 -- and any radio past the last slot is left untouched.
Gain comes from the slot, or from that radio's `overrides` entry if it has one.
See config/radio_plan.example.json.

Safe to run repeatedly. Stops SDRTrunk first if it's running (so it doesn't
overwrite our edits on quit), then relaunches it if we killed it.

Set SDRTRUNK_BIN to your sdr-trunk launcher path if the default
(/Applications/sdr-trunk/bin/sdr-trunk) doesn't match your install, and
SDRTRUNK_HOME if SDRTrunk's home dir isn't ~/SDRTrunk.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from radio_plan import (PlanError, apply_settings, hackrfs_in, load_plan,  # noqa: E402
                        rate_hz, slot_for)

SDRTRUNK_HOME = Path(os.environ.get("SDRTRUNK_HOME", Path.home() / "SDRTrunk"))
CFG = SDRTRUNK_HOME / "configuration" / "tuner_configuration.json"
SDRTRUNK_BIN = os.environ.get(
    "SDRTRUNK_BIN", "/Applications/sdr-trunk/bin/sdr-trunk")


def kill_sdrtrunk():
    """Kill running SDRTrunk if any, so it doesn't clobber our config on exit."""
    r = subprocess.run(
        ["pgrep", "-f", "io.github.dsheirer.gui.SDRTrunk"],
        capture_output=True, text=True)
    if not r.stdout.strip():
        return False
    subprocess.run(["sudo", "-n", "pkill", "-f", "io.github.dsheirer.gui.SDRTrunk"])
    time.sleep(2)
    return True


def launch_sdrtrunk():
    """Relaunch SDRTrunk via sudo -n with user.home preserved."""
    subprocess.Popen(
        ["sudo", "-n", "SDR_TRUNK_OPTS=-Duser.home=" + str(Path.home()),
         SDRTRUNK_BIN],
        stdout=open("/tmp/sdrtrunk-launch.log", "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True)


def tune(dry_run=False, no_restart=False):
    if not CFG.exists():
        print(f"config not found: {CFG}", file=sys.stderr); sys.exit(1)
    plan = load_plan()

    was_running = False
    if not no_restart:
        was_running = kill_sdrtrunk()
        if was_running:
            print("[tune] SDRTrunk killed (will relaunch)")

    backup = CFG.with_suffix(f".json.pre-tune.{int(time.time())}.backup")
    shutil.copy(CFG, backup)
    print(f"[tune] backup: {backup.name}")

    data = json.loads(CFG.read_text())
    hackrfs = hackrfs_in(data)
    print(f"[tune] {len(hackrfs)} HackRF(s) found, {len(plan['slots'])} slot(s) in plan")

    for i, tuner in enumerate(hackrfs):
        uid = tuner.get("uniqueID")
        settings = slot_for(plan, i, uid)
        if settings is None:
            print(f"  #{i+1} {uid} -> LEFT UNTOUCHED (no plan slot)")
            continue
        old_f, old_r = tuner.get("frequency"), tuner.get("sampleRate")
        apply_settings(tuner, settings)
        half = rate_hz(settings["sampleRate"]) / 2
        tag = " (gain override)" if uid in plan["overrides"] else ""
        print(f"  #{i+1} {uid} -> {settings['band']} "
              f"({settings['frequency']/1e6:.3f} MHz, {settings['sampleRate']}, "
              f"{(settings['frequency']-half)/1e6:.2f}-{(settings['frequency']+half)/1e6:.2f}); "
              f"amp={settings['amplifierEnabled']} "
              f"{settings['lnagain']}/{settings['vgagain']}{tag}; "
              f"was {(old_f or 0)/1e6:.3f} MHz {old_r}")

    if dry_run:
        print("[tune] --dry-run: not writing")
    else:
        CFG.write_text(json.dumps(data, indent=2))
        print(f"[tune] wrote: {CFG}")

    if was_running and not no_restart:
        launch_sdrtrunk()
        print("[tune] SDRTrunk relaunched")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-restart", action="store_true",
                    help="don't kill/relaunch SDRTrunk")
    try:
        tune(**vars(ap.parse_args()))
    except PlanError as e:
        print(e, file=sys.stderr); sys.exit(1)


if __name__ == "__main__":
    main()
