"""
Tail SDRTrunk event log CSVs and ingest EVERY row.

Each log filename encodes the channel: 20260310_133013.747_0_Hz_County-P25-East_call_events.log
Header row:
  TIMESTAMP,DURATION_MS,PROTOCOL,EVENT,FROM,TO,CHANNEL_NUMBER,FREQUENCY,TIMESLOT,DETAILS,EVENT_ID

We handle: log-rotate (new files appear), truncation, append.
"""
import csv
import io
import json
import os
import re
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from db import (db, upsert_radio, upsert_site, upsert_system, upsert_talkgroup,
                canonical_system)

# Per-system log files: every ingested event is also appended to
# ~/Desktop/trunk-tap-export/logs/<System>.log as a normalized TSV row so
# each radio system's traffic can be sorted/grepped on its own.
# (override root with SDRTD_EXPORT_DIR)
EXPORT_ROOT = Path(os.environ.get("SDRTD_EXPORT_DIR",
                                  Path.home() / "Desktop" / "trunk-tap-export"))
SYSLOG_DIR = EXPORT_ROOT / "logs"
SYSLOG_DIR.mkdir(parents=True, exist_ok=True)
_syslog_lock = threading.Lock()


def _syslog_name(label):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label.strip()) or "unknown"


def syslog_event(system_label, ts, event_type, from_rid, to_tgid, to_rid,
                 site, frequency, duration_ms, encrypted, details):
    line = "\t".join([
        time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
        event_type or "",
        str(from_rid or ""), str(to_tgid or ""), str(to_rid or ""),
        site or "", str(frequency or ""), str(duration_ms or ""),
        "ENC" if encrypted else "",
        (details or "").replace("\t", " ").replace("\n", " "),
    ]) + "\n"
    with _syslog_lock:
        with open(SYSLOG_DIR / f"{_syslog_name(system_label)}.log", "a") as f:
            f.write(line)


def write_transcript_file(system_label, ts, tgid, tg_alias, rid, call_id,
                          text, engine=None, model=None, lang=None, ms=None,
                          conf=None, audio_path=None, frequency=None,
                          encrypted=False):
    """Write one transcript to
    EXPORT_ROOT/transcripts/<System>/<YYYY-MM-DD>/<HHMMSS>_TG.._RID..._callN.txt
    with a metadata header. Returns the path written."""
    d = time.localtime(ts)
    day = time.strftime("%Y-%m-%d", d)
    hms = time.strftime("%H%M%S", d)
    alias_bit = f"_{_syslog_name(tg_alias)}" if tg_alias else ""
    fname = f"{hms}_TG{tgid or 0}{alias_bit}_RID{rid or 0}_call{call_id}.txt"
    outdir = EXPORT_ROOT / "transcripts" / _syslog_name(system_label) / day
    header = (f"# {time.strftime('%Y-%m-%dT%H:%M:%S', d)}  "
              f"system={system_label}  tg={tgid} \"{tg_alias or ''}\"  "
              f"rid={rid}  freq={frequency or ''}  enc={'yes' if encrypted else 'no'}\n"
              f"# engine={engine} model={model} lang={lang} ms={ms} conf={conf}\n"
              f"# audio={audio_path or ''}\n\n")
    with _syslog_lock:
        outdir.mkdir(parents=True, exist_ok=True)
        dest = outdir / fname
        dest.write_text(header + (text or "") + "\n")
    return dest


def _record_anomaly(ts, kind, system_id=None, site_id=None, rid=None, tgid=None, details=None, socketio=None):
    """Insert an anomaly row and (optionally) emit it over WS."""
    c = db()
    cur = c.execute("""INSERT INTO anomalies(ts, kind, system_id, site_id, rid, tgid, details)
                       VALUES (?,?,?,?,?,?,?)""",
                    (ts, kind, system_id, site_id, rid, tgid, details))
    if socketio is not None:
        socketio.emit("anomaly", {
            "id": cur.lastrowid, "ts": ts, "kind": kind,
            "system_id": system_id, "site_id": site_id,
            "rid": rid, "tgid": tgid, "details": details,
        })


def _detect_novelty(system_id, site_id, from_rid, to_tgid, ts, socketio=None):
    """Emit anomalies for genuinely first-seen combinations."""
    c = db()
    # These upserts have already run in ingest_row; check first_seen == ts to
    # know this is the first sighting.
    if from_rid is not None and system_id is not None:
        row = c.execute("SELECT first_seen FROM radios WHERE system_id=? AND rid=?",
                        (system_id, from_rid)).fetchone()
        if row and row["first_seen"] == ts:
            _record_anomaly(ts, "new_rid", system_id=system_id, rid=from_rid,
                            details=f"first sighting of RID {from_rid}",
                            socketio=socketio)

    if to_tgid is not None and system_id is not None:
        row = c.execute("SELECT first_seen FROM talkgroups WHERE system_id=? AND tgid=?",
                        (system_id, to_tgid)).fetchone()
        if row and row["first_seen"] == ts:
            _record_anomaly(ts, "new_tg", system_id=system_id, tgid=to_tgid,
                            details=f"first sighting of TG {to_tgid}",
                            socketio=socketio)

    if site_id is not None:
        row = c.execute("SELECT first_seen FROM sites WHERE id=?", (site_id,)).fetchone()
        if row and row["first_seen"] == ts:
            _record_anomaly(ts, "new_site", system_id=system_id, site_id=site_id,
                            details=f"first sighting of site",
                            socketio=socketio)

    # New RID on TG (dyad)
    if from_rid is not None and to_tgid is not None:
        seen = c.execute("""SELECT 1 FROM events WHERE from_rid=? AND to_tgid=?
                             AND ts < ? LIMIT 1""",
                         (from_rid, to_tgid, ts)).fetchone()
        if not seen:
            _record_anomaly(ts, "new_rid_on_tg", system_id=system_id,
                            rid=from_rid, tgid=to_tgid,
                            details=f"RID {from_rid} first heard on TG {to_tgid}",
                            socketio=socketio)

    # New RID at site (roaming to a site not previously seen)
    if from_rid is not None and site_id is not None:
        seen = c.execute("""SELECT 1 FROM roaming WHERE rid=? AND site_id=?
                             AND ts < ? LIMIT 1""",
                         (from_rid, site_id, ts)).fetchone()
        if not seen:
            _record_anomaly(ts, "new_rid_at_site", system_id=system_id,
                            site_id=site_id, rid=from_rid,
                            details=f"RID {from_rid} first observed at this site",
                            socketio=socketio)


def _spike_check(system_id, from_rid, ts, socketio=None, window_sec=60, threshold=8):
    """Flag a RID that transmits > threshold times in the last window."""
    if from_rid is None or system_id is None:
        return
    c = db()
    count = c.execute("""SELECT COUNT(*) FROM events WHERE system_id=? AND from_rid=?
                          AND ts BETWEEN ? AND ?""",
                      (system_id, from_rid, ts - window_sec, ts)).fetchone()[0]
    if count >= threshold:
        # Rate-limit: don't spam the same RID more than once per window
        last = c.execute("""SELECT ts FROM anomalies WHERE kind='spike' AND rid=? AND system_id=?
                          ORDER BY ts DESC LIMIT 1""", (from_rid, system_id)).fetchone()
        if last and (ts - last["ts"]) < window_sec:
            return
        _record_anomaly(ts, "spike", system_id=system_id, rid=from_rid,
                        details=f"RID {from_rid} sent {count} events in {window_sec}s",
                        socketio=socketio)

# ---- filename parsing --------------------------------------------------------
_FNAME_RE = re.compile(
    r"^(?P<date>\d{8})_(?P<time>\d{6}\.\d+)_\d+_Hz_(?P<label>.+?)_call_events\.log$"
)


def system_label_from_filename(fname):
    m = _FNAME_RE.match(fname)
    if not m:
        return None
    label = m.group("label").replace("-", " ").replace("_", " ").strip()
    return label


# ---- row parsing -------------------------------------------------------------
_TG_ALIAS_RE = re.compile(r"^\[(.+?)\]\s*\((\d+)\)")   # "[County EMS Dispatch] (3001)"
_TG_BARE_RE  = re.compile(r"^\s*\((\d+)\)")            # " (220026)"
_ID_ONLY_RE  = re.compile(r"^\s*(\d+)\s*$")


def _parse_actor(field):
    """Return (id, alias). Handles '[Label] (12345)', ' (12345)', '12345', ''."""
    if not field:
        return None, None
    field = field.strip()
    if not field:
        return None, None
    m = _TG_ALIAS_RE.match(field)
    if m:
        return int(m.group(2)), m.group(1)
    m = _TG_BARE_RE.match(field)
    if m:
        return int(m.group(1)), None
    m = _ID_ONLY_RE.match(field)
    if m:
        return int(m.group(1)), None
    return None, field


def _parse_ts(raw):
    # "2026:03:10:13:30:18"
    try:
        parts = raw.split(":")
        if len(parts) >= 6:
            import datetime as _dt
            dt = _dt.datetime(int(parts[0]), int(parts[1]), int(parts[2]),
                              int(parts[3]), int(parts[4]), int(parts[5]))
            return dt.timestamp()
    except Exception:
        pass
    return time.time()


def _int(x):
    try:
        return int(x) if x not in (None, "", " ") else None
    except (TypeError, ValueError):
        return None


def _freq_hz(x):
    """SDRTrunk logs frequency as MHz float e.g. 770.012500"""
    try:
        return int(float(x) * 1_000_000) if x not in (None, "", " ") else None
    except (TypeError, ValueError):
        return None


# ---- ingestion ---------------------------------------------------------------
def ingest_row(system_label, row, socketio=None):
    # Unify site-specific log names ("County P25 East/West") with the network name
    # used by RDIO uploads / playlist aliases ("Countywide") so events,
    # calls, logs and transcripts all share one system identity.
    system_label = canonical_system(system_label)
    ts = _parse_ts(row.get("TIMESTAMP", ""))
    duration = _int(row.get("DURATION_MS"))
    protocol = row.get("PROTOCOL") or "APCO-25"
    event_type = row.get("EVENT") or ""
    from_field = row.get("FROM") or ""
    to_field = row.get("TO") or ""
    channel = row.get("CHANNEL_NUMBER") or ""
    frequency = _freq_hz(row.get("FREQUENCY"))
    timeslot = _int(row.get("TIMESLOT"))
    details = row.get("DETAILS") or ""
    ev_id = row.get("EVENT_ID") or None

    if os.environ.get("SDRTD_DEBUG_TAIL") and "Call" in event_type:
        print(f"[debug] type={event_type!r} dur_raw={row.get('DURATION_MS')!r} "
              f"dur={duration!r}", flush=True)

    system_id = upsert_system(system_label, protocol=protocol, label=system_label, now=ts)

    # CHANNEL_NUMBER often carries the site id as "1-1" — capture site
    site_str = None
    if channel and "-" in channel:
        site_str = channel.split("-")[0]  # RFSS-SITE; keep just RFSS as site key
        # Better: keep full "1-1" as our site key
        site_str = channel
    site_id = upsert_site(system_id, site_str, name=site_str, now=ts) if site_str else None

    from_rid, from_alias = _parse_actor(from_field)
    to_id, to_alias = _parse_actor(to_field)

    # For Register/Response, SDRTrunk leaves FROM empty and puts the actor RID
    # in TO. Promote it to from_rid so downstream analytics see the actor.
    if event_type in ("Register", "Response") and from_rid is None and to_id is not None:
        from_rid = to_id
        to_id = None  # release TO -- affiliation TG comes from DETAILS

    # Distinguish TG-directed events from unit calls.
    to_tgid = None
    to_rid  = None
    if event_type in ("Group Call", "Data Call", "Response", "Register"):
        if event_type == "Group Call":
            to_tgid = to_id
        elif event_type == "Data Call":
            to_tgid = to_id
        elif event_type == "Response":
            # 'ACCEPTED AFFILIATION GROUP: 3001 (LOCAL) ANNOUNCEMENT GROUP:3000'
            m = re.search(r"AFFILIATION GROUP:\s*(\d+)", details)
            if m:
                to_tgid = int(m.group(1))
        elif event_type == "Register":
            m = re.search(r"GROUP:\s*(\d+)", details)
            if m:
                to_tgid = int(m.group(1))
    elif event_type == "Unit Call":
        to_rid = to_id

    encrypted = 0
    if "ENCRYPT" in details.upper() or "SVC OPT" in details.upper() and "ENC" in details.upper():
        encrypted = 1

    # upserts
    if to_tgid is not None:
        upsert_talkgroup(system_id, to_tgid, alias=to_alias, encrypted=bool(encrypted), now=ts)
    if from_rid is not None:
        upsert_radio(system_id, from_rid, alias=from_alias, now=ts)
    if to_rid is not None:
        upsert_radio(system_id, to_rid, alias=to_alias, now=ts)

    c = db()
    # Dedup by SDRTrunk's event_id (unique per event). SDRTrunk writes SEVERAL
    # log rows per event as the call progresses -- the first row (the grant)
    # usually has no DURATION_MS and no FROM; update rows fill them in later.
    # So on re-observation, merge the new info into the existing row instead
    # of just ignoring it, and re-emit when we learn who is talking.
    if ev_id is not None:
        existing = c.execute("SELECT id, duration_ms, from_rid FROM events "
                             "WHERE system_id=? AND event_id=?",
                             (system_id, ev_id)).fetchone()
    else:
        existing = None
    if existing is not None:
        learned_from = existing["from_rid"] is None and from_rid is not None
        c.execute("""UPDATE events SET
                         duration_ms = COALESCE(duration_ms, ?),
                         from_rid    = COALESCE(from_rid, ?),
                         to_tgid     = COALESCE(to_tgid, ?),
                         to_rid      = COALESCE(to_rid, ?),
                         frequency   = COALESCE(frequency, ?),
                         encrypted   = MAX(encrypted, ?)
                     WHERE id=?""",
                  (duration, from_rid, to_tgid, to_rid, frequency,
                   encrypted, existing["id"]))
        if learned_from and socketio is not None and time.time() - ts < 120:
            socketio.emit("event", {
                "ts": ts, "system": system_label, "type": event_type,
                "from": from_rid, "to_tg": to_tgid, "to_rid": to_rid,
                "site": site_str, "freq": frequency, "duration_ms": duration,
                "encrypted": bool(encrypted), "details": details,
            })
        return  # derived tables only on first sighting

    cur = c.execute("""INSERT INTO events(ts, system_id, site_id, protocol,
                            event_type, from_rid, to_tgid, to_rid, channel,
                            frequency, timeslot, duration_ms, details, event_id,
                            encrypted)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, system_id, site_id, protocol, event_type, from_rid,
                     to_tgid, to_rid, channel, frequency, timeslot,
                     duration, details, ev_id, encrypted))

    # Per-system log file (sortable TSV)
    try:
        syslog_event(system_label, ts, event_type, from_rid, to_tgid, to_rid,
                     site_str, frequency, duration, encrypted, details)
    except Exception as e:
        print(f"[syslog] err: {e!r}")

    # Side tables
    if event_type == "Response" and to_tgid is not None and from_rid is not None:
        c.execute("INSERT INTO affiliations(ts, system_id, site_id, rid, tgid) VALUES (?,?,?,?,?)",
                  (ts, system_id, site_id, from_rid, to_tgid))
    if site_id is not None and from_rid is not None:
        c.execute("INSERT INTO roaming(ts, system_id, site_id, rid) VALUES (?,?,?,?)",
                  (ts, system_id, site_id, from_rid))
    if "DENY" in event_type.upper() or "DENIED" in details.upper():
        c.execute("INSERT INTO denies(ts, system_id, site_id, rid, tgid, reason) VALUES (?,?,?,?,?,?)",
                  (ts, system_id, site_id, from_rid, to_tgid, details))
    if "ADJACENT" in details.upper():
        c.execute("INSERT INTO adjacent_sites(ts, system_id, from_site_id, neighbor_site, frequency) VALUES (?,?,?,?,?)",
                  (ts, system_id, site_id, details, frequency))
    if "PATCH" in event_type.upper():
        c.execute("INSERT INTO patches(ts, system_id, supergroup, child_tgs) VALUES (?,?,?,?)",
                  (ts, system_id, to_tgid, json.dumps({"details": details})))

    # Anomaly detection (novelty + spike). Runs after all upserts so first_seen
    # comparison is meaningful.
    try:
        _detect_novelty(system_id, site_id, from_rid, to_tgid, ts, socketio=socketio)
        _spike_check(system_id, from_rid, ts, socketio=socketio)
    except Exception as e:
        print(f"[anomaly] err: {e!r}")

    if socketio is not None:
        socketio.emit("event", {
            "ts": ts,
            "system": system_label,
            "type": event_type,
            "from": from_rid,
            "to_tg": to_tgid,
            "to_rid": to_rid,
            "site": site_str,
            "freq": frequency,
            "duration_ms": duration,
            "encrypted": bool(encrypted),
            "details": details,
        })


# ---- watcher -----------------------------------------------------------------
class _Tailer:
    def __init__(self, log_dir, socketio=None):
        self.log_dir = Path(log_dir)
        self.socketio = socketio
        self.offsets = {}       # path -> byte offset
        self.headers = {}       # path -> [col names]
        self.lock = threading.Lock()

    def _load_state(self, path):
        c = db()
        row = c.execute("SELECT offset FROM ingest_state WHERE path=?", (str(path),)).fetchone()
        return row["offset"] if row else 0

    def _save_state(self, path, offset):
        c = db()
        c.execute("""INSERT INTO ingest_state(path, offset, last_ts) VALUES(?,?,?)
                     ON CONFLICT(path) DO UPDATE SET offset=excluded.offset, last_ts=excluded.last_ts""",
                  (str(path), offset, time.time()))

    def scan_once(self):
        with self.lock:
            for path in sorted(self.log_dir.glob("*_call_events.log")):
                self._read_new(path)

    def _read_new(self, path):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        offset = self.offsets.get(str(path))
        if offset is None:
            offset = self._load_state(path)
        if offset > size:
            # truncated / rotated
            offset = 0
        if offset >= size:
            return

        system_label = system_label_from_filename(path.name) or path.stem

        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
            new_offset = f.tell()

        text = chunk.decode("utf-8", errors="replace")
        # If we started at 0, first line is header
        if offset == 0:
            first_nl = text.find("\n")
            if first_nl == -1:
                return
            header_line = text[:first_nl]
            self.headers[str(path)] = next(csv.reader(io.StringIO(header_line)))
            text = text[first_nl + 1:]
        cols = self.headers.get(str(path))
        if cols is None:
            # need header — try to load it from file start
            with open(path) as f:
                header_line = f.readline()
                cols = next(csv.reader(io.StringIO(header_line)))
                self.headers[str(path)] = cols

        # Process only complete lines; keep tail for next round
        last_nl = text.rfind("\n")
        if last_nl == -1:
            return
        complete = text[:last_nl + 1]
        remainder_bytes = len(text) - (last_nl + 1)
        new_offset -= remainder_bytes

        reader = csv.reader(io.StringIO(complete))
        for row_list in reader:
            if not row_list:
                continue
            row = dict(zip(cols, row_list))
            try:
                ingest_row(system_label, row, self.socketio)
            except Exception as e:
                print(f"[tail] parse error in {path.name}: {e!r}")

        self.offsets[str(path)] = new_offset
        self._save_state(path, new_offset)


class _FSEvents(FileSystemEventHandler):
    def __init__(self, tailer):
        self.tailer = tailer

    def on_created(self, event):
        if not event.is_directory:
            self.tailer.scan_once()

    def on_modified(self, event):
        if not event.is_directory:
            self.tailer.scan_once()


def start_tail(log_dir, socketio=None, poll_interval=1.0):
    tailer = _Tailer(log_dir, socketio=socketio)
    tailer.scan_once()

    observer = Observer()
    observer.schedule(_FSEvents(tailer), str(log_dir), recursive=False)
    observer.daemon = True
    observer.start()

    def _poll():
        while True:
            time.sleep(poll_interval)
            try:
                tailer.scan_once()
            except Exception as e:
                print(f"[tail] scan error: {e!r}")

    t = threading.Thread(target=_poll, name="sdr-tail-poll", daemon=True)
    t.start()
    return tailer
