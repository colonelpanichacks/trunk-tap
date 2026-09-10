"""
RDIO Scanner protocol endpoint.

SDRTrunk streams every completed trunked call here as a multipart/form-data POST
with the audio file plus metadata. Reference: https://github.com/chuot/rdio-scanner
Fields SDRTrunk sends:
    key, audio (file), audioName, audioType, dateTime, frequency, frequencies,
    patches, source, sources, system, systemLabel, talkgroup, talkgroupGroup,
    talkgroupLabel, talkgroupTag
"""
import json
import os
import time
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from db import db, upsert_radio, upsert_site, upsert_system, upsert_talkgroup

bp = Blueprint("rdio", __name__)

# Mutable state root (DB, audio, whisper models) -- a single volume in Docker.
DATA_DIR = Path(os.environ.get("SDRTD_DATA_DIR", Path(__file__).resolve().parents[1]))
AUDIO_DIR = DATA_DIR / "audio_calls"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def _int(x, default=None):
    try:
        if x is None or x == "":
            return default
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _jsonfield(x):
    if x is None or x == "":
        return None
    try:
        return json.loads(x) if isinstance(x, str) else x
    except json.JSONDecodeError:
        return None


@bp.route("/api/call-upload", methods=["GET", "POST"])
def call_upload():
    # SDRTrunk's RdioScannerBroadcaster pings this at startup as a health check
    # with just {key=...}; if there's no `audio` we return the same text an
    # actual Rdio Scanner server sends so SDRTrunk marks the target as reachable.
    if request.method == "GET":
        return ("Rdio Scanner API", 200, {"Content-Type": "text/plain"})
    form = request.form
    files = request.files
    # SDRTrunk's startup handshake: POST with a `test` field. Its
    # RdioScannerBroadcaster only marks the target CONNECTED when the reply
    # body starts with "incomplete call data: no talkgroup" (the real Rdio
    # Scanner server has no test endpoint, so SDRTrunk probes for that exact
    # error text). Anything else leaves it in ERROR and the queue never drains.
    if "test" in form:
        return ("Incomplete call data: no talkgroup", 200, {"Content-Type": "text/plain"})
    # No audio file OR only 'key' present -> incomplete
    has_audio = "audio" in request.files and request.files["audio"].filename
    payload_keys = [k for k in form.keys() if k != "key"]
    if not has_audio and not payload_keys:
        return ("incomplete call data: no talkgroup", 200, {"Content-Type": "text/plain"})

    # SDRTrunk sends form fields as strings
    key             = form.get("key", "")
    date_time       = _int(form.get("dateTime"))
    frequency       = _int(form.get("frequency"))
    frequencies     = _jsonfield(form.get("frequencies"))
    patches         = _jsonfield(form.get("patches"))
    source          = _int(form.get("source"))
    sources         = _jsonfield(form.get("sources"))
    system_label    = form.get("systemLabel") or form.get("system") or "unknown"
    talkgroup       = _int(form.get("talkgroup"))
    talkgroup_grp   = form.get("talkgroupGroup")
    talkgroup_label = form.get("talkgroupLabel")
    talkgroup_tag   = form.get("talkgroupTag")

    api_key_expected = current_app.config.get("RDIO_API_KEY")
    if api_key_expected and key != api_key_expected:
        return jsonify({"error": "bad api key"}), 401

    now = float(date_time) if date_time else time.time()
    system_id = upsert_system(system_label, protocol="APCO-25", label=system_label, now=now)

    # Dedupe: SDRTrunk occasionally POSTs the same call twice (queue retry /
    # broadcaster race). The RDIO protocol carries no call id, so the natural
    # key is (system, start ts, talkgroup, source radio). Reply with the same
    # success text so SDRTrunk drains its queue instead of retrying again.
    dup = db().execute("""SELECT id FROM calls
                           WHERE system_id=? AND ts=? AND tgid IS ? AND source_rid IS ?""",
                       (system_id, now, talkgroup, source)).fetchone()
    if dup:
        return ("Call imported successfully.", 200, {"Content-Type": "text/plain"})

    # SDRTrunk RDIO uploads don't include site id directly; we tag by systemLabel
    # and can join to the tail-ingest events by (system_id, ts) later.
    site_id = None

    # Encryption: RDIO Scanner protocol doesn't have an explicit "encrypted" flag,
    # but SDRTrunk names encrypted files or omits audio for enc calls; treat
    # zero-byte / missing audio as encrypted.
    audio = files.get("audio")
    audio_path = None
    audio_type = form.get("audioType")
    audio_bytes = 0
    encrypted = 0

    if audio and audio.filename:
        # Store under audio_calls/YYYY/MM/DD/<uuid>.<ext>
        ts_struct = time.gmtime(now)
        subdir = AUDIO_DIR / f"{ts_struct.tm_year:04d}" / f"{ts_struct.tm_mon:02d}" / f"{ts_struct.tm_mday:02d}"
        subdir.mkdir(parents=True, exist_ok=True)
        ext = Path(audio.filename).suffix or (".mp3" if audio_type == "audio/mpeg" else ".bin")
        fname = f"{uuid.uuid4().hex}{ext}"
        dest = subdir / fname
        audio.save(dest)
        audio_bytes = dest.stat().st_size
        audio_path = str(dest.relative_to(AUDIO_DIR.parent))
        if audio_bytes == 0:
            encrypted = 1
    else:
        encrypted = 1

    upsert_talkgroup(system_id, talkgroup, alias=talkgroup_label, tg_group=talkgroup_grp,
                     tg_tag=talkgroup_tag, encrypted=bool(encrypted), now=now,
                     add_call=1, add_ms=0)
    upsert_radio(system_id, source, now=now, add_call=1, add_ms=0)

    # Duration is not in the form — compute from audio only if present.
    duration_ms = None

    raw = {k: v for k, v in form.items()}
    if audio and audio.filename:
        raw["_audio_filename"] = audio.filename
        raw["_audio_bytes"] = audio_bytes

    transcribe_state = "pending" if (audio_path and not encrypted) else "skipped"

    c = db()
    cur = c.execute("""INSERT INTO calls(ts, system_id, site_id, talkgroup_id, tgid,
                                         source_rid, sources_json, frequency, frequencies_json,
                                         duration_ms, encrypted, patches_json, audio_path,
                                         audio_type, audio_bytes, raw_json, transcribe_state)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now, system_id, site_id, None, talkgroup, source,
                     json.dumps(sources) if sources else None,
                     frequency,
                     json.dumps(frequencies) if frequencies else None,
                     duration_ms, encrypted,
                     json.dumps(patches) if patches else None,
                     audio_path, audio_type, audio_bytes, json.dumps(raw),
                     transcribe_state))
    call_id = cur.lastrowid

    # Seed FTS row (transcript filled in when Whisper completes)
    c.execute("INSERT INTO calls_fts(rowid, transcript, tg_label, tg_group, system_label) VALUES (?,?,?,?,?)",
              (call_id, "", talkgroup_label or "", talkgroup_grp or "", system_label))

    # Per-system call log (sortable TSV, one line per call)
    try:
        from ingest.tail import SYSLOG_DIR, _syslog_lock, _syslog_name
        line = "\t".join([
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            str(talkgroup or ""), (talkgroup_label or ""), (talkgroup_grp or ""),
            str(source or ""), str(frequency or ""),
            "ENC" if encrypted else "", audio_path or "",
        ]) + "\n"
        with _syslog_lock:
            with open(SYSLOG_DIR / f"{_syslog_name(system_label)}.calls.log", "a") as f:
                f.write(line)
    except Exception as e:
        print(f"[syslog] call log err: {e!r}")

    # broadcast over websocket
    rid_alias = None
    if source is not None:
        r = c.execute("SELECT alias FROM radios WHERE system_id=? AND rid=?",
                      (system_id, source)).fetchone()
        rid_alias = r["alias"] if r else None
    from app import socketio  # local import to avoid circular
    socketio.emit("call", {
        "id": call_id,
        "ts": now,
        "system": system_label,
        "system_id": system_id,
        "tgid": talkgroup,
        "tg_label": talkgroup_label,
        "tg_group": talkgroup_grp,
        "source": source,
        "rid_alias": rid_alias,
        "frequency": frequency,
        "encrypted": bool(encrypted),
        "audio": audio_path,
    })

    # SDRTrunk's RdioScannerBroadcaster looks for this exact text to mark the
    # upload as successful (matches the real Rdio Scanner server response).
    return ("Call imported successfully.", 200, {"Content-Type": "text/plain"})


@bp.route("/api/audio/<path:relpath>")
def serve_audio(relpath):
    from flask import send_from_directory
    return send_from_directory(AUDIO_DIR.parent, relpath, as_attachment=False)
