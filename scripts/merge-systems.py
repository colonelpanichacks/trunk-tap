#!/usr/bin/env python3
"""Merge one or more `systems` rows into a single canonical system.

Usage: ./.venv/bin/python scripts/merge-systems.py "Countywide" "County P25 East" "County P25 West"

Needed because SDRTrunk gives the same network multiple identities: event-log
filenames are site-specific ("County P25 East") while RDIO call uploads and
playlist aliases use the network name ("Countywide"). Going forward,
canonical_system() (rules in config/systems.json) canonicalizes at ingest
time; this script repairs the
rows that already landed under the split identities.

Merges: talkgroups/radios (counts summed, first/last_seen min/maxed, alias
fields COALESCEd), sites (by site_id, FKs re-pointed), and every table with a
plain system_id column. Source system rows are deleted at the end.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db

# tables with a plain system_id column (no row-merge needed, just re-point)
FLAT_TABLES = ["events", "calls", "affiliations", "roaming", "patches",
               "denies", "adjacent_sites", "anomalies"]
# tables holding a sites.id FK that must follow a site merge
SITE_FK = [("events", "site_id"), ("calls", "site_id"),
           ("affiliations", "site_id"), ("roaming", "site_id"),
           ("denies", "site_id"), ("anomalies", "site_id"),
           ("adjacent_sites", "from_site_id")]


def merge_systems(target_name, source_names):
    c = db()
    tgt = c.execute("SELECT id FROM systems WHERE name=?", (target_name,)).fetchone()
    if not tgt:
        raise SystemExit(f"target system not found: {target_name!r}")
    tid = tgt["id"]

    for src_name in source_names:
        src = c.execute("SELECT id FROM systems WHERE name=?", (src_name,)).fetchone()
        if not src:
            print(f"[skip] no system named {src_name!r}")
            continue
        sid = src["id"]
        if sid == tid:
            print(f"[skip] {src_name!r} is the target")
            continue
        print(f"[merge] {src_name!r} (id={sid}) -> {target_name!r} (id={tid})")

        # --- sites: merge by site_id, re-point FKs from dead rows ----------
        site_map = {}
        for s in c.execute("SELECT * FROM sites WHERE system_id=?", (sid,)).fetchall():
            keep = c.execute("SELECT id FROM sites WHERE system_id=? AND site_id=?",
                             (tid, s["site_id"])).fetchone()
            if keep:
                c.execute("""UPDATE sites SET first_seen=MIN(first_seen, ?),
                                             last_seen=MAX(last_seen, ?),
                                             name=COALESCE(name, ?)
                             WHERE id=?""",
                          (s["first_seen"], s["last_seen"], s["name"], keep["id"]))
                site_map[s["id"]] = keep["id"]
                c.execute("DELETE FROM sites WHERE id=?", (s["id"],))
            else:
                c.execute("UPDATE sites SET system_id=? WHERE id=?", (tid, s["id"]))
                site_map[s["id"]] = s["id"]
        for table, col in SITE_FK:
            for old, new in site_map.items():
                if old != new:
                    c.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))

        # --- talkgroups: merge by tgid --------------------------------------
        for tg in c.execute("SELECT * FROM talkgroups WHERE system_id=?", (sid,)).fetchall():
            keep = c.execute("SELECT id FROM talkgroups WHERE system_id=? AND tgid=?",
                             (tid, tg["tgid"])).fetchone()
            if keep:
                c.execute("""UPDATE talkgroups SET
                                alias=COALESCE(alias, ?),
                                tg_group=COALESCE(tg_group, ?),
                                tg_tag=COALESCE(tg_tag, ?),
                                priority=COALESCE(priority, ?),
                                encrypted=MAX(encrypted, ?),
                                call_count=call_count + ?,
                                total_ms=total_ms + ?,
                                first_seen=MIN(first_seen, ?),
                                last_seen=MAX(last_seen, ?)
                             WHERE id=?""",
                          (tg["alias"], tg["tg_group"], tg["tg_tag"], tg["priority"],
                           tg["encrypted"], tg["call_count"], tg["total_ms"],
                           tg["first_seen"], tg["last_seen"], keep["id"]))
                c.execute("UPDATE calls SET talkgroup_id=? WHERE talkgroup_id=?",
                          (keep["id"], tg["id"]))
                c.execute("DELETE FROM talkgroups WHERE id=?", (tg["id"],))
            else:
                c.execute("UPDATE talkgroups SET system_id=? WHERE id=?", (tid, tg["id"]))

        # --- radios: merge by rid -------------------------------------------
        for r in c.execute("SELECT * FROM radios WHERE system_id=?", (sid,)).fetchall():
            keep = c.execute("SELECT id FROM radios WHERE system_id=? AND rid=?",
                             (tid, r["rid"])).fetchone()
            if keep:
                c.execute("""UPDATE radios SET
                                alias=COALESCE(alias, ?),
                                call_count=call_count + ?,
                                total_ms=total_ms + ?,
                                first_seen=MIN(first_seen, ?),
                                last_seen=MAX(last_seen, ?)
                             WHERE id=?""",
                          (r["alias"], r["call_count"], r["total_ms"],
                           r["first_seen"], r["last_seen"], keep["id"]))
                c.execute("DELETE FROM radios WHERE id=?", (r["id"],))
            else:
                c.execute("UPDATE radios SET system_id=? WHERE id=?", (tid, r["id"]))

        # --- flat tables: just re-point system_id ----------------------------
        for t in FLAT_TABLES:
            n = c.execute(f"UPDATE OR IGNORE {t} SET system_id=? WHERE system_id=?",
                          (tid, sid)).rowcount
            leftover = c.execute(f"SELECT COUNT(*) FROM {t} WHERE system_id=?",
                                 (sid,)).fetchone()[0]
            if leftover:  # unique-index collision (shouldn't happen for events)
                print(f"  [warn] {t}: {leftover} rows collide on unique key; dropped")
                c.execute(f"DELETE FROM {t} WHERE system_id=?", (sid,))
            elif n:
                print(f"  {t}: {n} rows")

        c.execute("DELETE FROM systems WHERE id=?", (sid,))
        print(f"[done] {src_name!r} merged")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    merge_systems(sys.argv[1], sys.argv[2:])
