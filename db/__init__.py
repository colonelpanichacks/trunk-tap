import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_DIR = Path(__file__).parent
# All mutable state (DB, audio, whisper models) lives under SDRTD_DATA_DIR so
# Docker can mount it as a volume. Default: the project directory.
DATA_DIR = Path(os.environ.get("SDRTD_DATA_DIR", Path(__file__).resolve().parents[1]))
DB_PATH = DATA_DIR / "db" / "sdrtrunk.db"
SCHEMA_PATH = DB_DIR / "schema.sql"

_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _connect():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        conn.close()
        _INITIALIZED = True


def db():
    if not _INITIALIZED:
        init_db()
    if not hasattr(_LOCAL, "conn"):
        _LOCAL.conn = _connect()
    return _LOCAL.conn


# ---- upsert helpers ----------------------------------------------------------

# Distinct SDRTrunk identities that are really the same network. Event-log
# filenames give site-specific names ("County P25 East/West", "State P25 Site 700")
# while RDIO call uploads and playlist aliases use the network name
# ("Countywide", "State P25") -- unify on the latter so calls, events,
# talkgroups, logs and transcripts all land under one system identity.
# Rules live in config/systems.json (see config/systems.example.json); a
# missing/invalid file means passthrough (every name is its own identity).
SYSTEMS_CONFIG_PATH = Path(os.environ.get(
    "SDRTD_SYSTEMS_CONFIG",
    Path(__file__).resolve().parents[1] / "config" / "systems.json"))

_cfg_cache = {"mtime": None, "data": {}}


def load_systems_config():
    """Parsed config/systems.json ({} on missing/invalid), mtime-cached."""
    try:
        mtime = SYSTEMS_CONFIG_PATH.stat().st_mtime
    except OSError:
        return {}
    if _cfg_cache["mtime"] != mtime:
        try:
            data = json.loads(SYSTEMS_CONFIG_PATH.read_text())
            _cfg_cache["data"] = data if isinstance(data, dict) else {}
        except Exception:
            _cfg_cache["data"] = {}
        _cfg_cache["mtime"] = mtime
    return _cfg_cache["data"]


def canonical_system(name):
    if not name:
        return name
    for rule in load_systems_config().get("canonical_rules", []):
        exact = rule.get("exact")
        if exact and name in exact:
            return exact[name]
        prefix = rule.get("prefix")
        if prefix and name.startswith(prefix):
            return rule.get("canonical", name)
    return name


def upsert_system(name, protocol=None, label=None, now=None):
    name = canonical_system(name)
    now = now or time.time()
    c = db()
    row = c.execute("SELECT id FROM systems WHERE name=?", (name,)).fetchone()
    if row:
        c.execute("UPDATE systems SET last_seen=?, protocol=COALESCE(?,protocol), label=COALESCE(?,label) WHERE id=?",
                  (now, protocol, label, row["id"]))
        return row["id"]
    cur = c.execute("INSERT INTO systems(name, protocol, label, first_seen, last_seen) VALUES (?,?,?,?,?)",
                    (name, protocol, label, now, now))
    return cur.lastrowid


def upsert_site(system_id, site_id, name=None, rfss=None, wacn=None, now=None):
    if system_id is None or site_id is None:
        return None
    now = now or time.time()
    c = db()
    row = c.execute("SELECT id FROM sites WHERE system_id=? AND site_id=?", (system_id, site_id)).fetchone()
    if row:
        c.execute("UPDATE sites SET last_seen=?, name=COALESCE(?,name), rfss=COALESCE(?,rfss), wacn=COALESCE(?,wacn) WHERE id=?",
                  (now, name, rfss, wacn, row["id"]))
        return row["id"]
    cur = c.execute("INSERT INTO sites(system_id, site_id, name, rfss, wacn, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                    (system_id, site_id, name, rfss, wacn, now, now))
    return cur.lastrowid


def upsert_talkgroup(system_id, tgid, alias=None, tg_group=None, tg_tag=None, priority=None,
                     encrypted=None, now=None, add_call=0, add_ms=0):
    if system_id is None or tgid is None:
        return None
    now = now or time.time()
    c = db()
    row = c.execute("SELECT id, encrypted FROM talkgroups WHERE system_id=? AND tgid=?",
                    (system_id, tgid)).fetchone()
    if row:
        new_enc = row["encrypted"] or (1 if encrypted else 0)
        c.execute("""UPDATE talkgroups
                        SET last_seen=?,
                            alias=COALESCE(?,alias),
                            tg_group=COALESCE(?,tg_group),
                            tg_tag=COALESCE(?,tg_tag),
                            priority=COALESCE(?,priority),
                            encrypted=?,
                            call_count=call_count+?,
                            total_ms=total_ms+?
                      WHERE id=?""",
                  (now, alias, tg_group, tg_tag, priority, new_enc, add_call, add_ms, row["id"]))
        return row["id"]
    cur = c.execute("""INSERT INTO talkgroups(system_id, tgid, alias, tg_group, tg_tag, priority,
                                              encrypted, call_count, total_ms, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (system_id, tgid, alias, tg_group, tg_tag, priority,
                     1 if encrypted else 0, add_call, add_ms, now, now))
    return cur.lastrowid


def upsert_radio(system_id, rid, alias=None, now=None, add_call=0, add_ms=0):
    if system_id is None or rid is None:
        return None
    now = now or time.time()
    c = db()
    row = c.execute("SELECT id FROM radios WHERE system_id=? AND rid=?", (system_id, rid)).fetchone()
    if row:
        c.execute("""UPDATE radios SET last_seen=?, alias=COALESCE(?,alias),
                                       call_count=call_count+?, total_ms=total_ms+?
                     WHERE id=?""",
                  (now, alias, add_call, add_ms, row["id"]))
        return row["id"]
    cur = c.execute("""INSERT INTO radios(system_id, rid, alias, call_count, total_ms, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?)""",
                    (system_id, rid, alias, add_call, add_ms, now, now))
    return cur.lastrowid


def wipe():
    """Nuke the database file. Caller reinitializes via init_db()."""
    global _INITIALIZED
    with _INIT_LOCK:
        try:
            if hasattr(_LOCAL, "conn"):
                _LOCAL.conn.close()
                del _LOCAL.conn
        except Exception:
            pass
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        _INITIALIZED = False
