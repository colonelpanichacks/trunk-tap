"""
Import aliases from ~/SDRTrunk/playlist/default.xml so the dashboard shows
"County PD Dispatch" and not just talkgroup 2001.

Every alias in the playlist maps a talkgroup id to a human name, group ("Law",
"County Fire", "County EMS", ...), priority, and alias-list name (which we
treat as the system label: "Countywide", "State P25", "City P25").
"""
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from db import db, upsert_system, upsert_talkgroup

_SDRTRUNK_HOME = Path(os.environ.get("SDRTRUNK_HOME", Path.home() / "SDRTrunk"))
DEFAULT_PLAYLIST = _SDRTRUNK_HOME / "playlist" / "default.xml"

# alias_list_name (from channels) -> system label we use in DB
LIST_TO_SYSTEM = {
    "Countywide": "Countywide",
    "City P25": "City P25",
    "State P25 Region": "State P25",
}


def import_from(path=DEFAULT_PLAYLIST):
    path = Path(path)
    if not path.exists():
        return {"imported": 0, "error": f"not found: {path}"}
    tree = ET.parse(path)
    root = tree.getroot()

    n = 0
    for alias in root.findall("alias"):
        list_name = alias.get("list", "")
        system_label = LIST_TO_SYSTEM.get(list_name, list_name or "unknown")
        system_id = upsert_system(system_label)

        name = alias.get("name")
        group = alias.get("group")

        tgid = None
        priority = None
        for aid in alias.findall("id"):
            t = aid.get("type")
            if t == "talkgroup":
                try:
                    tgid = int(aid.get("value"))
                except (TypeError, ValueError):
                    pass
            elif t == "priority":
                try:
                    priority = int(aid.get("priority"))
                except (TypeError, ValueError):
                    pass

        if tgid is None:
            continue

        upsert_talkgroup(system_id, tgid, alias=name, tg_group=group,
                         priority=priority)
        n += 1
    return {"imported": n, "path": str(path)}


if __name__ == "__main__":
    import sys, json
    print(json.dumps(import_from(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLAYLIST), indent=2))
