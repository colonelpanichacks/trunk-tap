#!/usr/bin/env python3
"""
Patch ~/SDRTrunk/playlist/default.xml to add an RDIO Scanner streaming target
pointed at this dashboard, and tag every alias so its calls broadcast to it.

Idempotent: safe to run repeatedly. Always writes a timestamped backup first.
"""
import argparse
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

PLAYLIST = Path.home() / "SDRTrunk" / "playlist" / "default.xml"
# SDRTrunk uses Jackson polymorphic XML, not JAXB. Type discriminator is a plain
# "type" attribute (Jackson @JsonTypeInfo, no xsi namespace).


def patch(host="http://127.0.0.1:5544/api/call-upload", api_key="", system_id=1,
          name="dashboard", playlist=PLAYLIST):
    playlist = Path(playlist)
    if not playlist.exists():
        print(f"playlist not found: {playlist}", file=sys.stderr)
        sys.exit(1)

    backup = playlist.with_suffix(f".xml.pre-stream.{int(time.time())}.backup")
    shutil.copy(playlist, backup)
    print(f"backup written: {backup.name}")

    tree = ET.parse(playlist)
    root = tree.getroot()

    # ---- 1. RDIO Scanner <stream> element ---------------------------------
    existing = [s for s in root.findall("stream")
                if s.get("type") == "RdioScannerConfiguration"
                and s.get("name") == name]
    if existing:
        stream = existing[0]
        print(f"stream '{name}' already present -- updating")
    else:
        stream = ET.SubElement(root, "stream")
        print(f"adding stream '{name}'")

    stream.set("type", "RdioScannerConfiguration")
    stream.set("broadcaster", "RDIOSCANNER_CALL")
    stream.set("enabled", "true")
    stream.set("format", "MP3")
    stream.set("name", name)
    stream.set("host", host)
    stream.set("port", "80")
    stream.set("delay", "0")
    stream.set("maximum_recording_age", "600000")
    stream.set("api_key", api_key)
    stream.set("system_id", str(system_id))

    # ---- 2. Tag every alias with a broadcastChannel identifier -----------
    # Jackson type id is 'broadcastChannel' (case matters -- not 'broadcast').
    tagged = 0
    already = 0
    for alias in root.findall("alias"):
        has = False
        for ident in alias.findall("id"):
            if ident.get("type") == "broadcastChannel" and ident.get("channel") == name:
                has = True
                break
        if has:
            already += 1
            continue
        bc = ET.SubElement(alias, "id")
        bc.set("type", "broadcastChannel")
        bc.set("channel", name)
        tagged += 1
    print(f"aliases: {tagged} tagged now, {already} already had it")

    # ---- 3. Save --------------------------------------------------------------
    tree.write(playlist, xml_declaration=False, encoding="utf-8")
    print(f"wrote: {playlist}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://127.0.0.1:5544/api/call-upload")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--system-id", type=int, default=1)
    ap.add_argument("--name", default="dashboard")
    ap.add_argument("--playlist", default=str(PLAYLIST))
    args = ap.parse_args()
    patch(host=args.host, api_key=args.api_key, system_id=args.system_id,
          name=args.name, playlist=args.playlist)


if __name__ == "__main__":
    main()
