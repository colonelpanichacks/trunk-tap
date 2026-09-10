#!/usr/bin/env python3
"""
Shared radio plan: band slots + per-radio gain, used by both tune-hackrfs.py
and radio-autopilot.py so they can't drift apart on band strategy.

Plan comes from config/radio_plan.json (copy config/radio_plan.example.json),
override the path with SDRTD_RADIO_PLAN.

Slot assignment:
  1. A slot with a "uniqueID" claims that radio wherever it sits in the file.
  2. Remaining slots are filled with the remaining radios in uniqueID order.
So you can pin the radio on the 700 MHz antenna to the 700 MHz slot and let
the rest sort themselves out.
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
    """Return {"slots": [...], "overrides": {...}}, validated."""
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
            "pin": s.get("uniqueID") or None,
            "frequency": s["freq_hz"],
            "sampleRate": s.get("sample_rate", "RATE_10_0"),
            "autoPPMCorrectionEnabled": True,
            **_gains(s, f"slot {i}"),
        })
    if not slots:
        raise PlanError(f"{p} defines no slots")

    pins = [s["pin"] for s in slots if s["pin"]]
    if len(pins) != len(set(pins)):
        raise PlanError("two slots pin the same uniqueID")

    overrides = {uid: _gains(g, f"override {uid}")
                 for uid, g in (raw.get("overrides") or {}).items()}
    return {"slots": slots, "overrides": overrides}


# ---- radio identity ----------------------------------------------------------

def norm_id(value):
    """Normalize a HackRF identity for comparison. SDRTrunk writes the serial
    as four hex groups ("A1B2C3D4-...."); the USB bus reports the same digits
    unseparated and usually lowercase, with leading zeros. Reduce both to bare
    significant hex."""
    if not value:
        return ""
    return re.sub(r"[^0-9a-f]", "", str(value).lower()).lstrip("0")


def same_radio(a, b):
    """Do these two identities refer to one radio? Suffix-tolerant, because
    some tools report only the low half of the serial."""
    a, b = norm_id(a), norm_id(b)
    if not a or not b:
        return False
    return a == b or a.endswith(b) or b.endswith(a)


# ---- slot assignment ---------------------------------------------------------

def _settings(plan, slot, unique_id):
    out = dict(slot)   # "band"/"pin" are metadata; apply_settings skips them
    out.update(plan["overrides"].get(unique_id, {}))
    for uid, gains in plan["overrides"].items():
        if uid != unique_id and same_radio(uid, unique_id):
            out.update(gains)
    return out


def unmatched_pins(plan, tuners):
    """Slot pins matching no tuner config -- a typo, or a radio SDRTrunk has
    never seen. Those slots fall back to being filled in uniqueID order, so
    the band still gets covered, but the caller should say so out loud."""
    return [s["pin"] for s in plan["slots"]
            if s["pin"] and not any(same_radio(s["pin"], t.get("uniqueID"))
                                    for t in tuners)]


def assign_slots(plan, tuners, present=None):
    """Map tuner configs to plan slots.

    `present` is the list of serials seen on the USB bus, or None when we
    couldn't enumerate them. Returns [(tuner, settings|None, live)] in the
    order given, where `live` says the radio is physically plugged in.
    """
    slots = plan["slots"]
    claimed_slot, claimed_tuner = {}, set()

    for si, slot in enumerate(slots):
        if not slot["pin"]:
            continue
        for ti, t in enumerate(tuners):
            if ti not in claimed_tuner and same_radio(slot["pin"], t.get("uniqueID")):
                claimed_slot[ti] = si
                claimed_tuner.add(ti)
                break

    free_slots = [i for i in range(len(slots)) if i not in claimed_slot.values()]
    free_tuners = [i for i in range(len(tuners)) if i not in claimed_tuner]
    for ti, si in zip(free_tuners, free_slots):
        claimed_slot[ti] = si

    out = []
    for ti, t in enumerate(tuners):
        si = claimed_slot.get(ti)
        settings = _settings(plan, slots[si], t.get("uniqueID")) if si is not None else None
        if present is None:
            # No serials available: assume the first N radios in the file are
            # the ones plugged in (N is filled in by the caller).
            live = None
        else:
            live = any(same_radio(t.get("uniqueID"), s) for s in present)
        out.append((t, settings, live))
    return out


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
        if k in ("band", "pin"):
            continue
        if tuner.get(k) != v:
            tuner[k] = v
            changed = True
    return changed


def needs_change(tuner, settings):
    return any(tuner.get(k) != v for k, v in settings.items()
               if k not in ("band", "pin"))


def hackrfs_in(data):
    """HackRF tuner configs, in the uniqueID order slots are assigned by."""
    return sorted((t for t in data.get("tunerConfigurations", [])
                   if t.get("type") == "hackRFTunerConfiguration"),
                  key=lambda t: t.get("uniqueID", ""))


# ---- USB enumeration (never opens the device -- SDRTrunk may hold it) --------

_IOREG_SERIAL = re.compile(r'"USB Serial Number"\s*=\s*"([^"]+)"')


def _serials_darwin():
    out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
                         capture_output=True, text=True).stdout
    serials, in_hackrf = [], False
    for line in out.splitlines():
        if "+-o " in line:
            in_hackrf = "HackRF" in line
        elif in_hackrf:
            m = _IOREG_SERIAL.search(line)
            if m:
                serials.append(m.group(1))
                in_hackrf = False
    return serials


def _serials_sysfs():
    serials = []
    for dev in sorted(Path("/sys/bus/usb/devices").glob("*")):
        try:
            vid = (dev / "idVendor").read_text().strip().lower()
            pid = (dev / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if (vid, pid) not in USB_IDS:
            continue
        try:
            serials.append((dev / "serial").read_text().strip())
        except OSError:
            serials.append("")   # present but serial unreadable
    return serials


def _count_lsusb():
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True).stdout.lower()
    except FileNotFoundError:
        raise PlanError("cannot enumerate USB: no /sys/bus/usb and no lsusb")
    return sum(out.count(f"{vid}:{pid}") for vid, pid in USB_IDS)


def hackrf_serials():
    """Serials of the HackRFs on the bus. Empty strings for radios we can see
    but can't identify; None when the platform gives us no serials at all."""
    if sys.platform == "darwin":
        return _serials_darwin()
    if Path("/sys/bus/usb/devices").is_dir():
        return _serials_sysfs()
    return None


def hackrf_count():
    serials = hackrf_serials()
    return _count_lsusb() if serials is None else len(serials)


def usable_serials(serials):
    """Serials good enough to match radios by. All-or-nothing: if any radio on
    the bus is anonymous we fall back to counting, rather than deciding a
    plugged-in radio is absent."""
    if not serials or any(not norm_id(s) for s in serials):
        return None
    return serials


if __name__ == "__main__":
    try:
        plan = load_plan()
    except PlanError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    serials = hackrf_serials()
    n = len(serials) if serials is not None else hackrf_count()
    print(f"{n} HackRF(s) on the bus; {len(plan['slots'])} slot(s) in the plan")
    if serials:
        for s in serials:
            print(f"  bus: {s or '(serial unreadable)'}")
    if serials is not None and usable_serials(serials) is None and n:
        print("  note: serials unreadable, falling back to counting radios")

    for i, s in enumerate(plan["slots"]):
        half = rate_hz(s["sampleRate"]) / 2
        pin = f"  pinned to {s['pin']}" if s["pin"] else ""
        print(f"  slot {i}: {s['band']:<32} {s['frequency']/1e6:>8.3f} MHz "
              f"({(s['frequency']-half)/1e6:.2f}-{(s['frequency']+half)/1e6:.2f})  "
              f"amp={s['amplifierEnabled']} {s['lnagain']}/{s['vgagain']}{pin}")
    for uid, g in plan["overrides"].items():
        print(f"  override {uid}: amp={g['amplifierEnabled']} {g['lnagain']}/{g['vgagain']}")
