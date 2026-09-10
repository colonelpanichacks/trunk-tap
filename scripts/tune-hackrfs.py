#!/usr/bin/env python3
"""
Auto-tune every HackRF in ~/SDRTrunk/configuration/tuner_configuration.json
for optimal 700/800 MHz P25 trunked-system coverage.

Assignment strategy:
  1st HackRF (by unique-id sort)  ->  772 MHz center, 10 MHz sample rate
      (covers 767-777 MHz: the 700 MHz band where most P25 simulcasts live)
  2nd HackRF                       ->  855 MHz center, 10 MHz sample rate
      (covers 850-860 MHz: the 800 MHz band -- more trunked sites plus
       800 MHz Interop 8CALL90/8TAC91-94)
  3rd+ HackRFs                     ->  left untouched

Adjust PLAN below (or the band centers) to fit your 700 MHz trunked systems.

Also sets amplifier=on, LNA=24, VGA=30 (good gain profile for weak simulcast),
autoPPMCorrectionEnabled=true.

Safe to run repeatedly. Stops SDRTrunk first if it's running (so it doesn't
overwrite our edits on quit), then relaunches it if we killed it.

Set SDRTRUNK_BIN to your sdr-trunk launcher path if the default
(/Applications/sdr-trunk/bin/sdr-trunk) doesn't match your install.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CFG = Path.home() / "SDRTrunk" / "configuration" / "tuner_configuration.json"
SDRTRUNK_BIN = os.environ.get(
    "SDRTRUNK_BIN", "/Applications/sdr-trunk/bin/sdr-trunk")

PLAN = [
    dict(band="700 MHz", freq=772_000_000, rate="RATE_10_0"),
    dict(band="800 MHz", freq=855_000_000, rate="RATE_10_0"),
]


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

    was_running = False
    if not no_restart:
        was_running = kill_sdrtrunk()
        if was_running:
            print("[tune] SDRTrunk killed (will relaunch)")

    backup = CFG.with_suffix(f".json.pre-tune.{int(time.time())}.backup")
    shutil.copy(CFG, backup)
    print(f"[tune] backup: {backup.name}")

    data = json.loads(CFG.read_text())
    hackrfs = [t for t in data.get("tunerConfigurations", [])
               if t.get("type") == "hackRFTunerConfiguration"]
    hackrfs.sort(key=lambda t: t.get("uniqueID", ""))
    print(f"[tune] {len(hackrfs)} HackRF(s) found")

    for i, tuner in enumerate(hackrfs):
        plan = PLAN[i] if i < len(PLAN) else None
        if plan is None:
            print(f"  #{i+1} {tuner.get('uniqueID')} -> LEFT UNTOUCHED (no plan slot)")
            continue
        old_f = tuner.get("frequency")
        old_r = tuner.get("sampleRate")
        tuner["frequency"] = plan["freq"]
        tuner["sampleRate"] = plan["rate"]
        tuner["amplifierEnabled"] = True
        tuner["lnagain"] = "GAIN_24"
        tuner["vgagain"] = "GAIN_30"
        tuner["autoPPMCorrectionEnabled"] = True
        print(f"  #{i+1} {tuner.get('uniqueID')} -> {plan['band']} "
              f"({plan['freq']/1e6:.3f} MHz, {plan['rate']}); "
              f"was {old_f/1e6:.3f} MHz {old_r}")

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
    tune(**vars(ap.parse_args()))


if __name__ == "__main__":
    main()
