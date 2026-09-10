#!/usr/bin/env python3
"""
Shared radio plan: band slots + per-radio gain, used by both tune-hackrfs.py
and radio-autopilot.py so they can't drift apart on band strategy.

Plan comes from config/radio_plan.json (copy config/radio_plan.example.json),
override the path with SDRTD_RADIO_PLAN.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = Path(os.environ.get("SDRTD_RADIO_PLAN", ROOT / "config" / "radio_plan.json"))
EXAMPLE_PATH = ROOT / "config" / "radio_plan.example.json"

# HackRF One, Jawbreaker, rad1o -- all Great Scott Gadgets VID 1d50.
USB_IDS = {("1d50", "6089"), ("1d50", "604b"), ("1d50", "cc15")}


class PlanError(Exception):
    pass


# ---- plan loading ------------------------------------------------------------

def _gain_enum(kind, value, lo, hi, step):
    """HackRF gains are enum strings in SDRTrunk's config ("GAIN_24")."""
    if not isinstance(value, int) or not lo <= value <= hi or value % step:
        raise PlanError(f"{kind} must be an int {lo}-{hi} in steps of {step}, got {value!r}")
    return f"GAIN_{value}"


def _gains(src, where):
    return {
        "amplifierEnabled": bool(src.get("amp", True)),
        "lnagain": _gain_enum(f"{where} lna", src.get("lna", 24), 0, 40, 8),
        "vgagain": _gain_enum(f"{where} vga", src.get("vga", 30), 0, 62, 2),
    }


def load_plan(path=None):
    """Return {"slots": [...], "overrides": {uniqueID: gains}}, validated."""
    p = Path(path) if path else PLAN_PATH
    if not p.exists():
        raise PlanError(f"no radio plan at {p}\ncopy {EXAMPLE_PATH.name} to {p.name} and edit it")
    raw = json.loads(p.read_text())

    slots = []
    for i, s in enumerate(raw.get("slots") or []):
        if not isinstance(s.get("freq_hz"), int):
            raise PlanError(f"slot {i}: freq_hz must be an integer in Hz")
        slots.append({
            "band": s.get("band") or f"slot {i}",
            "frequency": s["freq_hz"],
            "sampleRate": s.get("sample_rate", "RATE_10_0"),
            "autoPPMCorrectionEnabled": True,
            **_gains(s, f"slot {i}"),
        })
    if not slots:
        raise PlanError(f"{p} defines no slots")

    overrides = {uid: _gains(g, f"override {uid}")
                 for uid, g in (raw.get("overrides") or {}).items()}
    return {"slots": slots, "overrides": overrides}


def slot_for(plan, index, unique_id):
    """Settings for the index-th radio, with any per-uniqueID gain override
    applied. None when the plan has no slot that far out."""
    if index >= len(plan["slots"]):
        return None
    return {**plan["slots"][index], **plan["overrides"].get(unique_id, {})}


def rate_hz(rate):
    """"RATE_10_0" -> 10000000. Falls back to 10 MHz on anything unparseable."""
    m = re.match(r"RATE_(\d+)_(\d+)$", str(rate))
    return int(float(f"{m.group(1)}.{m.group(2)}") * 1e6) if m else 10_000_000


def covers(settings, freq_hz):
    """Is freq_hz inside this tuner's receive window (center +/- rate/2)?"""
    half = rate_hz(settings["sampleRate"]) / 2
    return abs(freq_hz - settings["frequency"]) <= half


def apply_settings(tuner, settings):
    """Write settings onto a tuner config dict. True if anything changed."""
    changed = False
    for k, v in settings.items():
        if k == "band":
            continue
        if tuner.get(k) != v:
            tuner[k] = v
            changed = True
    return changed


def hackrfs_in(data):
    """HackRF tuner configs, in the uniqueID order slots are assigned by."""
    return sorted((t for t in data.get("tunerConfigurations", [])
                   if t.get("type") == "hackRFTunerConfiguration"),
                  key=lambda t: t.get("uniqueID", ""))


# ---- USB enumeration (never opens the device -- SDRTrunk may hold it) --------

def _count_darwin():
    out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
                         capture_output=True, text=True).stdout
    return out.count("HackRF One@") + out.count("HackRF Jawbreaker@")


def _count_sysfs():
    n = 0
    for dev in Path("/sys/bus/usb/devices").glob("*"):
        try:
            vid = (dev / "idVendor").read_text().strip().lower()
            pid = (dev / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if (vid, pid) in USB_IDS:
            n += 1
    return n


def _count_lsusb():
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise PlanError("cannot enumerate USB: no /sys/bus/usb and no lsusb")
    return sum(1 for vid, pid in USB_IDS if f"{vid}:{pid}" in out.lower())


def hackrf_count():
    """Number of HackRFs on the USB bus. macOS uses ioreg, Linux prefers sysfs
    so usbutils isn't a hard dependency."""
    if sys.platform == "darwin":
        return _count_darwin()
    if Path("/sys/bus/usb/devices").is_dir():
        return _count_sysfs()
    return _count_lsusb()


if __name__ == "__main__":
    try:
        plan = load_plan()
    except PlanError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    print(f"{hackrf_count()} HackRF(s) on the bus; {len(plan['slots'])} slot(s) in the plan")
    for i, s in enumerate(plan["slots"]):
        lo = (s["frequency"] - rate_hz(s["sampleRate"]) / 2) / 1e6
        hi = (s["frequency"] + rate_hz(s["sampleRate"]) / 2) / 1e6
        print(f"  slot {i}: {s['band']:<32} {s['frequency']/1e6:>8.3f} MHz "
              f"({lo:.2f}-{hi:.2f})  amp={s['amplifierEnabled']} "
              f"{s['lnagain']}/{s['vgagain']}")
    for uid, g in plan["overrides"].items():
        print(f"  override {uid}: amp={g['amplifierEnabled']} {g['lnagain']}/{g['vgagain']}")
