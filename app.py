"""
trunk-tap main app.

  - HTTP endpoints for the RDIO Scanner protocol (SDRTrunk pushes calls here)
  - Live tail of SDRTrunk event log CSVs (control-channel metadata)
  - Whisper worker transcribes every unencrypted call
  - Flask-SocketIO fanout to the web UI
  - REST/query API for graphs and history
"""
import argparse
import json
import os
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

from db import db, init_db, load_systems_config, wipe as db_wipe
from ingest.rdio import bp as rdio_bp
from ingest.tail import start_tail
from ingest.whisper_worker import start_worker as start_whisper
from ingest import aliases as aliases_mod

# SDRTrunk's data dir (event logs, playlist, tuner configuration). Override
# with SDRTRUNK_HOME -- in Docker this is the /sdrtrunk mount.
SDRTRUNK_HOME = os.environ.get("SDRTRUNK_HOME", str(Path.home() / "SDRTrunk"))
DEFAULT_LOG_DIR = str(Path(SDRTRUNK_HOME) / "event_logs")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024  # audio uploads
app.config["RDIO_API_KEY"] = os.environ.get("SDRTD_RDIO_KEY", "")

# threading mode: eventlet on Python 3.14 is deprecated/unstable (silent drops
# of emits from background threads, boot hangs). threading + simple-websocket
# is reliable at this dashboard's scale and handles emits from the tailer and
# whisper worker threads natively.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

app.register_blueprint(rdio_bp)


@app.after_request
def _no_cache(resp):
    # Dev dashboard -- always serve fresh, browsers were sticking on stale HTML/JS.
    if resp.mimetype in ("text/html", "application/javascript", "text/css"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ---- static UI ---------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html",
                           rdio_key=app.config["RDIO_API_KEY"] or "(none)")


# ---- read APIs ---------------------------------------------------------------
@app.route("/api/config")
def api_config():
    """Identity config for the front end: RadioReference system ids and the
    canonical-name rules (same rules canonical_system() applies at ingest)."""
    cfg = load_systems_config()
    return jsonify({"rr_sids": cfg.get("rr_sids", {}),
                    "canonical_rules": cfg.get("canonical_rules", [])})


@app.route("/api/systems")
def api_systems():
    rows = db().execute("SELECT id, name, protocol, first_seen, last_seen FROM systems ORDER BY name").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/talkgroups")
def api_talkgroups():
    system = request.args.get("system")
    limit = int(request.args.get("limit", 200))
    q = """SELECT tg.tgid, tg.alias, tg.tg_group, tg.priority, tg.encrypted,
                  tg.call_count, tg.total_ms, tg.first_seen, tg.last_seen,
                  s.name AS system
             FROM talkgroups tg JOIN systems s ON tg.system_id=s.id"""
    params = []
    if system:
        q += " WHERE s.name = ?"
        params.append(system)
    q += " ORDER BY tg.call_count DESC LIMIT ?"
    params.append(limit)
    return jsonify([dict(r) for r in db().execute(q, params).fetchall()])


@app.route("/api/radios")
def api_radios():
    system = request.args.get("system")
    limit = int(request.args.get("limit", 200))
    q = """SELECT r.rid, r.alias, r.call_count, r.total_ms, r.first_seen, r.last_seen,
                  s.name AS system
             FROM radios r JOIN systems s ON r.system_id=s.id"""
    params = []
    if system:
        q += " WHERE s.name = ?"
        params.append(system)
    q += " ORDER BY r.call_count DESC LIMIT ?"
    params.append(limit)
    return jsonify([dict(r) for r in db().execute(q, params).fetchall()])


@app.route("/api/calls")
def api_calls():
    limit = int(request.args.get("limit", 100))
    since = float(request.args.get("since", 0))
    system = request.args.get("system")
    q = """SELECT c.id, c.ts, c.tgid, c.source_rid, c.frequency, c.duration_ms,
                  c.encrypted, c.audio_path, c.transcript, c.transcribe_state,
                  s.name AS system, tg.alias AS tg_alias, tg.tg_group,
                  r.alias AS rid_alias
             FROM calls c
             LEFT JOIN systems s      ON c.system_id=s.id
             LEFT JOIN talkgroups tg  ON tg.system_id=c.system_id AND tg.tgid=c.tgid
             LEFT JOIN radios r       ON r.system_id=c.system_id AND r.rid=c.source_rid
             WHERE c.ts >= ?"""
    params = [since]
    if system:
        q += " AND s.name = ?"
        params.append(system)
    q += " ORDER BY c.ts DESC LIMIT ?"
    params.append(limit)
    return jsonify([dict(r) for r in db().execute(q, params).fetchall()])


@app.route("/api/events")
def api_events():
    limit = int(request.args.get("limit", 200))
    since = float(request.args.get("since", 0))
    system = request.args.get("system")
    q = """SELECT e.ts, e.event_type, e.from_rid, e.to_tgid, e.to_rid, e.channel,
                  e.frequency, e.duration_ms, e.details, e.encrypted, s.name AS system
             FROM events e LEFT JOIN systems s ON e.system_id=s.id
             WHERE e.ts >= ?"""
    params = [since]
    if system:
        q += " AND s.name = ?"
        params.append(system)
    q += " ORDER BY e.ts DESC LIMIT ?"
    params.append(limit)
    return jsonify([dict(r) for r in db().execute(q, params).fetchall()])


@app.route("/api/stats/summary")
def api_summary():
    c = db()
    def q(sql, *a):
        r = c.execute(sql, a).fetchone()
        return r[0] if r else 0
    return jsonify({
        "calls":         q("SELECT COUNT(*) FROM calls"),
        "events":        q("SELECT COUNT(*) FROM events"),
        "talkgroups":    q("SELECT COUNT(*) FROM talkgroups"),
        "radios":        q("SELECT COUNT(*) FROM radios"),
        "sites":         q("SELECT COUNT(*) FROM sites"),
        "systems":       q("SELECT COUNT(*) FROM systems"),
        "encrypted_calls": q("SELECT COUNT(*) FROM calls WHERE encrypted=1"),
        "transcribed":   q("SELECT COUNT(*) FROM calls WHERE transcribe_state='done'"),
        "pending_transcripts": q("SELECT COUNT(*) FROM calls WHERE transcribe_state='pending'"),
    })


@app.route("/api/stats/by_hour")
def api_by_hour():
    hours = int(request.args.get("hours", 24))
    since = time.time() - hours * 3600
    rows = db().execute("""SELECT CAST(ts/3600 AS INTEGER) AS hour, COUNT(*) AS n,
                                  SUM(encrypted) AS enc
                             FROM calls WHERE ts >= ?
                            GROUP BY hour ORDER BY hour""", (since,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stats/tg_heatmap")
def api_tg_heatmap():
    hours = int(request.args.get("hours", 24))
    top = int(request.args.get("top", 30))
    since = time.time() - hours * 3600
    tgs = db().execute("""SELECT c.tgid, tg.alias, COUNT(*) AS n
                            FROM calls c LEFT JOIN talkgroups tg
                              ON tg.system_id=c.system_id AND tg.tgid=c.tgid
                           WHERE c.ts >= ?
                        GROUP BY c.tgid ORDER BY n DESC LIMIT ?""",
                       (since, top)).fetchall()
    tgids = [r["tgid"] for r in tgs]
    if not tgids:
        return jsonify({"tgs": [], "cells": []})
    ph = ",".join("?" * len(tgids))
    cells = db().execute(f"""SELECT CAST(ts/3600 AS INTEGER) AS hour, tgid, COUNT(*) AS n
                              FROM calls WHERE ts >= ? AND tgid IN ({ph})
                          GROUP BY hour, tgid""",
                         [since] + tgids).fetchall()
    return jsonify({
        "tgs": [dict(r) for r in tgs],
        "cells": [dict(r) for r in cells],
    })


@app.route("/api/graph/topology")
def api_graph_topology():
    """Full network topology: systems -- sites, systems -- talkgroups,
    RIDs -- TGs (transmissions), RIDs -- sites (sightings). Everything we
    have detected as connected within the window, weighted by event count."""
    hours = float(request.args.get("hours", 24))
    since = time.time() - hours * 3600
    c = db()
    nodes, edges = {}, {}

    tg_alias = {(r["system_id"], r["tgid"]): r["alias"]
                for r in c.execute("SELECT system_id, tgid, alias FROM talkgroups")}
    rid_alias = {(r["system_id"], r["rid"]): r["alias"]
                 for r in c.execute("SELECT system_id, rid, alias FROM radios")}

    def add_node(nid, kind, label, n=0, title="", dbid=None):
        if nid in nodes:
            nodes[nid]["n"] += n
        else:
            nodes[nid] = {"id": nid, "kind": kind, "label": label,
                          "n": n, "title": title, "dbid": dbid}

    def add_edge(a, b, w, kind):
        if not a or not b or a == b:
            return
        key = (a, b)
        if key in edges:
            edges[key]["w"] += w
        else:
            edges[key] = {"from": a, "to": b, "w": w, "kind": kind}

    for r in c.execute("SELECT id, name, protocol FROM systems"):
        add_node(f"sys:{r['id']}", "system", r["name"],
                 title=f"{r['name']} ({r['protocol'] or '?'})", dbid=r["id"])

    for r in c.execute("""SELECT s.id, s.system_id, s.site_id, s.name,
                                 (SELECT COUNT(*) FROM events e
                                   WHERE e.site_id=s.id AND e.ts>=?) AS n
                            FROM sites s WHERE s.last_seen >= ?""",
                       (since, since)):
        if r["n"] == 0:
            continue
        add_node(f"site:{r['id']}", "site", r["name"] or r["site_id"] or "site",
                 n=r["n"], title=f"site {r['site_id'] or ''}", dbid=r["id"])
        if r["system_id"]:
            add_edge(f"sys:{r['system_id']}", f"site:{r['id']}", r["n"], "member")

    for r in c.execute("""SELECT e.system_id, e.to_tgid AS tgid, COUNT(*) AS n
                            FROM events e
                           WHERE e.ts >= ? AND e.to_tgid IS NOT NULL
                           GROUP BY e.system_id, e.to_tgid
                           ORDER BY n DESC LIMIT 60""", (since,)):
        sid, tgid = r["system_id"], r["tgid"]
        add_node(f"tg:{sid}:{tgid}", "tg",
                 tg_alias.get((sid, tgid)) or f"TG {tgid}",
                 n=r["n"], title=f"TG {tgid}", dbid=tgid)
        add_edge(f"sys:{sid}", f"tg:{sid}:{tgid}", r["n"], "member")

    for r in c.execute("""SELECT e.system_id, e.from_rid AS rid, e.to_tgid AS tgid,
                                 COUNT(*) AS n
                            FROM events e
                           WHERE e.ts >= ? AND e.from_rid IS NOT NULL
                             AND e.to_tgid IS NOT NULL
                           GROUP BY e.system_id, e.from_rid, e.to_tgid
                           ORDER BY n DESC LIMIT 150""", (since,)):
        sid, rid, tgid = r["system_id"], r["rid"], r["tgid"]
        rn, tn = f"rid:{sid}:{rid}", f"tg:{sid}:{tgid}"
        if rn not in nodes:
            add_node(rn, "rid", rid_alias.get((sid, rid)) or f"RID {rid}",
                     title=f"RID {rid}", dbid=rid)
        if tn not in nodes:
            add_node(tn, "tg", tg_alias.get((sid, tgid)) or f"TG {tgid}",
                     title=f"TG {tgid}", dbid=tgid)
            add_edge(f"sys:{sid}", tn, 0, "member")
        nodes[rn]["n"] += r["n"]
        add_edge(rn, tn, r["n"], "tx")

    for r in c.execute("""SELECT e.system_id, e.site_id, e.from_rid AS rid,
                                 COUNT(*) AS n
                            FROM events e
                           WHERE e.ts >= ? AND e.from_rid IS NOT NULL
                             AND e.site_id IS NOT NULL
                           GROUP BY e.system_id, e.site_id, e.from_rid
                           ORDER BY n DESC LIMIT 150""", (since,)):
        sid, rid = r["system_id"], r["rid"]
        rn, sn = f"rid:{sid}:{rid}", f"site:{r['site_id']}"
        if sn not in nodes:
            continue
        if rn not in nodes:
            add_node(rn, "rid", rid_alias.get((sid, rid)) or f"RID {rid}",
                     title=f"RID {rid}", dbid=rid)
        add_edge(rn, sn, r["n"], "seen")

    # Keep only nodes that participate in at least one edge -- the pane shows
    # what we have actually detected as connected, not every known entity.
    connected = set()
    for e in edges.values():
        connected.add(e["from"])
        connected.add(e["to"])
    node_list = [n for nid, n in nodes.items() if nid in connected]

    return jsonify({"nodes": node_list, "edges": list(edges.values())})


@app.route("/api/site/<int:sid>")
def api_site_detail(sid):
    c = db()
    info = c.execute("""SELECT s.*, sys.name AS system FROM sites s
                        LEFT JOIN systems sys ON sys.id=s.system_id
                        WHERE s.id=?""", (sid,)).fetchone()
    if not info:
        return jsonify({"error": "no such site"}), 404
    top_rids = c.execute("""SELECT from_rid AS rid, COUNT(*) AS n, MAX(ts) AS last_ts
                              FROM events WHERE site_id=? AND from_rid IS NOT NULL
                              GROUP BY from_rid ORDER BY n DESC LIMIT 25""", (sid,)).fetchall()
    top_tgs = c.execute("""SELECT to_tgid AS tgid, COUNT(*) AS n, MAX(ts) AS last_ts
                             FROM events WHERE site_id=? AND to_tgid IS NOT NULL
                             GROUP BY to_tgid ORDER BY n DESC LIMIT 25""", (sid,)).fetchall()
    return jsonify({"info": dict(info),
                    "top_rids": [dict(r) for r in top_rids],
                    "top_tgs": [dict(r) for r in top_tgs]})


@app.route("/api/system/<int:sid>")
def api_system_detail(sid):
    c = db()
    info = c.execute("SELECT * FROM systems WHERE id=?", (sid,)).fetchone()
    if not info:
        return jsonify({"error": "no such system"}), 404
    counts = {
        "talkgroups": c.execute("SELECT COUNT(*) FROM talkgroups WHERE system_id=?", (sid,)).fetchone()[0],
        "radios":     c.execute("SELECT COUNT(*) FROM radios WHERE system_id=?", (sid,)).fetchone()[0],
        "calls":      c.execute("SELECT COUNT(*) FROM calls WHERE system_id=?", (sid,)).fetchone()[0],
        "events":     c.execute("SELECT COUNT(*) FROM events WHERE system_id=?", (sid,)).fetchone()[0],
        "encrypted_calls": c.execute("SELECT COUNT(*) FROM calls WHERE system_id=? AND encrypted=1", (sid,)).fetchone()[0],
    }
    top_tgs = c.execute("""SELECT tgid, alias, call_count, encrypted
                             FROM talkgroups WHERE system_id=?
                             ORDER BY call_count DESC LIMIT 25""", (sid,)).fetchall()
    return jsonify({"info": dict(info), "counts": counts,
                    "top_tgs": [dict(r) for r in top_tgs]})


@app.route("/api/graph/rid_tg")
def api_rid_tg_graph():
    """Bipartite graph: RID -- TG edges weighted by call count."""
    limit = int(request.args.get("limit", 200))
    edges = db().execute("""SELECT source_rid AS rid, tgid, COUNT(*) AS w
                              FROM calls
                             WHERE source_rid IS NOT NULL AND tgid IS NOT NULL
                          GROUP BY source_rid, tgid
                          ORDER BY w DESC LIMIT ?""", (limit,)).fetchall()
    return jsonify([dict(r) for r in edges])


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    limit = int(request.args.get("limit", 50))
    rows = db().execute(
        """SELECT c.id, c.ts, c.tgid, c.source_rid, c.encrypted, c.audio_path,
                  c.transcript, s.name AS system, tg.alias AS tg_alias
             FROM calls_fts f
             JOIN calls c ON c.id=f.rowid
             LEFT JOIN systems s ON c.system_id=s.id
             LEFT JOIN talkgroups tg ON tg.system_id=c.system_id AND tg.tgid=c.tgid
            WHERE calls_fts MATCH ?
            ORDER BY c.ts DESC LIMIT ?""",
        (q, limit)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/import_aliases", methods=["POST"])
def api_import_aliases():
    return jsonify(aliases_mod.import_from())


@app.route("/api/alias", methods=["POST"])
def api_set_alias():
    """Set/clear a single talkgroup or radio alias from the dashboard UI.
    Blank alias clears (NULL). Unknown entities get a stub row so the alias
    sticks for when the TG/RID first appears on the air."""
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind not in ("tg", "rid"):
        return jsonify({"error": "bad kind"}), 400
    system = (data.get("system") or "").strip()
    try:
        ent_id = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad id"}), 400
    alias = (data.get("alias") or "").strip() or None
    c = db()
    row = c.execute("SELECT id FROM systems WHERE name=?", (system,)).fetchone()
    if not row:
        return jsonify({"error": "unknown system"}), 404
    sid = row["id"]
    now = time.time()
    if kind == "tg":
        cur = c.execute("UPDATE talkgroups SET alias=? WHERE system_id=? AND tgid=?",
                        (alias, sid, ent_id))
        if cur.rowcount == 0:
            c.execute("""INSERT INTO talkgroups(system_id, tgid, alias, first_seen, last_seen)
                         VALUES (?,?,?,?,?)""", (sid, ent_id, alias, now, now))
    else:
        cur = c.execute("UPDATE radios SET alias=? WHERE system_id=? AND rid=?",
                        (alias, sid, ent_id))
        if cur.rowcount == 0:
            c.execute("""INSERT INTO radios(system_id, rid, alias, first_seen, last_seen)
                         VALUES (?,?,?,?,?)""", (sid, ent_id, alias, now, now))
    return jsonify({"ok": True, "kind": kind, "system": system, "id": ent_id, "alias": alias})


@app.route("/api/sites")
def api_sites():
    """Per-site stats: call/event count, encryption ratio, first/last seen."""
    rows = db().execute("""
        SELECT s.id, s.site_id, s.name AS site_name, sys.name AS system,
               s.first_seen, s.last_seen,
               (SELECT COUNT(*) FROM events e WHERE e.site_id=s.id) AS events,
               (SELECT COUNT(*) FROM calls c WHERE c.site_id=s.id) AS calls,
               (SELECT COUNT(*) FROM events e WHERE e.site_id=s.id AND e.encrypted=1) AS enc_events,
               (SELECT COUNT(DISTINCT rid) FROM roaming r WHERE r.site_id=s.id) AS unique_rids
          FROM sites s LEFT JOIN systems sys ON s.system_id=sys.id
      ORDER BY events DESC""").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/denies")
def api_denies():
    limit = int(request.args.get("limit", 200))
    rows = db().execute("""
        SELECT d.ts, sys.name AS system, s.name AS site, d.rid, d.tgid,
               tg.alias AS tg_alias, d.reason
          FROM denies d
          LEFT JOIN systems sys ON d.system_id=sys.id
          LEFT JOIN sites s     ON d.site_id=s.id
          LEFT JOIN talkgroups tg ON tg.system_id=d.system_id AND tg.tgid=d.tgid
      ORDER BY d.ts DESC LIMIT ?""", (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/affiliations")
def api_affiliations():
    """Latest TG affiliation per RID (or all history if ?rid= given)."""
    rid = request.args.get("rid", type=int)
    if rid is not None:
        rows = db().execute("""
            SELECT a.ts, sys.name AS system, s.name AS site, a.rid, a.tgid,
                   tg.alias AS tg_alias
              FROM affiliations a
              LEFT JOIN systems sys ON a.system_id=sys.id
              LEFT JOIN sites s     ON a.site_id=s.id
              LEFT JOIN talkgroups tg ON tg.system_id=a.system_id AND tg.tgid=a.tgid
             WHERE a.rid=?
          ORDER BY a.ts DESC LIMIT 500""", (rid,)).fetchall()
    else:
        rows = db().execute("""
            SELECT a.rid, a.tgid, tg.alias AS tg_alias, sys.name AS system,
                   MAX(a.ts) AS last_ts, COUNT(*) AS n
              FROM affiliations a
              LEFT JOIN systems sys ON a.system_id=sys.id
              LEFT JOIN talkgroups tg ON tg.system_id=a.system_id AND tg.tgid=a.tgid
          GROUP BY a.rid, a.tgid ORDER BY last_ts DESC LIMIT 500""").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/roaming/<int:rid>")
def api_roaming_rid(rid):
    """Sites a specific RID has been observed at, most recent first."""
    rows = db().execute("""
        SELECT r.ts, s.name AS site, sys.name AS system
          FROM roaming r
          LEFT JOIN sites s   ON r.site_id=s.id
          LEFT JOIN systems sys ON r.system_id=sys.id
         WHERE r.rid=?
      ORDER BY r.ts DESC LIMIT 500""", (rid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/adjacent_sites")
def api_adjacent_sites():
    rows = db().execute("""
        SELECT a.ts, sys.name AS system, s.name AS from_site, a.neighbor_site, a.frequency
          FROM adjacent_sites a
          LEFT JOIN systems sys ON a.system_id=sys.id
          LEFT JOIN sites s     ON a.from_site_id=s.id
      ORDER BY a.ts DESC LIMIT 500""").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stats/encryption")
def api_stats_encryption():
    """Encryption breakdown: per system, per TG, per hour."""
    c = db()
    per_system = c.execute("""
        SELECT sys.name AS system,
               SUM(CASE WHEN e.encrypted=1 THEN 1 ELSE 0 END) AS enc,
               COUNT(*) AS total
          FROM events e JOIN systems sys ON e.system_id=sys.id
      GROUP BY sys.name ORDER BY total DESC""").fetchall()
    per_tg = c.execute("""
        SELECT tg.tgid, tg.alias, tg.tg_group, sys.name AS system, tg.call_count,
               tg.encrypted
          FROM talkgroups tg JOIN systems sys ON tg.system_id=sys.id
         WHERE tg.encrypted=1
      ORDER BY tg.call_count DESC LIMIT 100""").fetchall()
    return jsonify({
        "per_system": [dict(r) for r in per_system],
        "encrypted_talkgroups": [dict(r) for r in per_tg],
    })


@app.route("/api/stats/site_load")
def api_stats_site_load():
    hours = int(request.args.get("hours", 24))
    since = time.time() - hours * 3600
    rows = db().execute("""
        SELECT s.name AS site, sys.name AS system,
               CAST(e.ts/3600 AS INTEGER) AS hour, COUNT(*) AS n
          FROM events e
          JOIN sites s   ON e.site_id=s.id
          JOIN systems sys ON e.system_id=sys.id
         WHERE e.ts >= ?
      GROUP BY s.id, hour ORDER BY hour""", (since,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/rid/<int:rid>")
def api_rid_profile(rid):
    """Full profile for one radio: calls, affiliations, roaming sites, TG partners."""
    c = db()
    info = c.execute("""SELECT r.rid, r.alias, r.call_count, r.total_ms,
                               r.first_seen, r.last_seen, sys.name AS system
                          FROM radios r JOIN systems sys ON r.system_id=sys.id
                         WHERE r.rid=?""", (rid,)).fetchone()
    if not info:
        return jsonify({"error": "no such rid"}), 404
    calls = c.execute("""SELECT ts, tgid, encrypted, frequency, duration_ms, transcript
                           FROM calls WHERE source_rid=? ORDER BY ts DESC LIMIT 200""",
                      (rid,)).fetchall()
    affs = c.execute("""SELECT tgid, COUNT(*) AS n, MAX(ts) AS last_ts
                          FROM affiliations WHERE rid=? GROUP BY tgid ORDER BY n DESC""",
                     (rid,)).fetchall()
    sites = c.execute("""SELECT s.name AS site, COUNT(*) AS n, MAX(r.ts) AS last_ts
                           FROM roaming r JOIN sites s ON r.site_id=s.id
                          WHERE r.rid=? GROUP BY s.id ORDER BY n DESC""",
                      (rid,)).fetchall()
    tg_partners = c.execute("""SELECT tgid, COUNT(*) AS n
                                 FROM calls WHERE source_rid=? AND tgid IS NOT NULL
                             GROUP BY tgid ORDER BY n DESC LIMIT 20""", (rid,)).fetchall()
    return jsonify({
        "info": dict(info),
        "calls": [dict(r) for r in calls],
        "affiliations": [dict(r) for r in affs],
        "sites": [dict(r) for r in sites],
        "tg_partners": [dict(r) for r in tg_partners],
    })


@app.route("/api/anomalies")
def api_anomalies():
    """Recent anomalies (new RIDs, new TGs, spikes, novel roaming, etc)."""
    limit = int(request.args.get("limit", 200))
    kind = request.args.get("kind")
    system = request.args.get("system")
    hours = float(request.args.get("hours", 0))
    q = """SELECT a.id, a.ts, a.kind, a.rid, a.tgid, a.details, a.ack,
                  sys.name AS system, s.name AS site
             FROM anomalies a
             LEFT JOIN systems sys ON a.system_id=sys.id
             LEFT JOIN sites s     ON a.site_id=s.id
            WHERE 1=1"""
    params = []
    if kind:
        q += " AND a.kind = ?"
        params.append(kind)
    if system:
        q += " AND sys.name = ?"
        params.append(system)
    if hours > 0:
        q += " AND a.ts >= ?"
        params.append(time.time() - hours * 3600)
    q += " ORDER BY a.ts DESC LIMIT ?"
    params.append(limit)
    return jsonify([dict(r) for r in db().execute(q, params).fetchall()])


@app.route("/api/anomalies/stats")
def api_anomalies_stats():
    """Aggregate counts per kind, plus new-RID growth curve for last 24h.
    per_kind honors the same hours/system filters as the alerts table."""
    hours = float(request.args.get("hours", 0))
    system = request.args.get("system")
    c = db()
    q = """SELECT kind, COUNT(*) AS n
             FROM anomalies a LEFT JOIN systems sys ON a.system_id=sys.id"""
    cond, params = [], []
    if hours > 0:
        cond.append("a.ts >= ?")
        params.append(time.time() - hours * 3600)
    if system:
        cond.append("sys.name = ?")
        params.append(system)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " GROUP BY kind ORDER BY n DESC"
    per_kind = c.execute(q, params).fetchall()
    kinds = c.execute("SELECT DISTINCT kind FROM anomalies ORDER BY kind").fetchall()
    since = time.time() - 24*3600
    growth = c.execute("""SELECT CAST(ts/3600 AS INTEGER) AS hour, COUNT(*) AS n
                            FROM anomalies WHERE kind='new_rid' AND ts >= ?
                        GROUP BY hour ORDER BY hour""", (since,)).fetchall()
    return jsonify({
        "per_kind": [dict(r) for r in per_kind],
        "kinds": [r["kind"] for r in kinds],
        "new_rid_by_hour": [dict(r) for r in growth],
    })


@app.route("/api/whos_talking")
def api_whos_talking():
    """Recent RID -> TG dyads: who's actively speaking on what, most recent first.
    Groups by (RID, TG) with count of transmissions in the last N minutes."""
    minutes = int(request.args.get("minutes", 15))
    since = time.time() - minutes * 60
    rows = db().execute("""
        SELECT e.from_rid AS rid, e.to_tgid AS tgid,
               COUNT(*) AS n, MAX(e.ts) AS last_ts,
               SUM(CASE WHEN e.encrypted=1 THEN 1 ELSE 0 END) AS enc,
               SUM(COALESCE(e.duration_ms,0)) AS total_ms,
               sys.name AS system,
               tg.alias AS tg_alias, tg.tg_group,
               r.alias AS rid_alias,
               r.first_seen AS rid_first_seen, tg.first_seen AS tg_first_seen,
               (SELECT c2.transcript FROM calls c2
                 WHERE c2.system_id=e.system_id AND c2.source_rid=e.from_rid
                   AND c2.tgid=e.to_tgid AND c2.transcript IS NOT NULL
                 ORDER BY c2.ts DESC LIMIT 1) AS last_tx,
               (SELECT e2.frequency FROM events e2
                 WHERE e2.system_id=e.system_id AND e2.from_rid=e.from_rid
                   AND e2.to_tgid=e.to_tgid AND e2.frequency IS NOT NULL
                 ORDER BY e2.ts DESC LIMIT 1) AS last_freq
          FROM events e
          LEFT JOIN systems sys   ON e.system_id=sys.id
          LEFT JOIN talkgroups tg ON tg.system_id=e.system_id AND tg.tgid=e.to_tgid
          LEFT JOIN radios r      ON r.system_id=e.system_id AND r.rid=e.from_rid
         WHERE e.from_rid IS NOT NULL AND e.to_tgid IS NOT NULL
           AND e.ts >= ?
      GROUP BY e.from_rid, e.to_tgid
      ORDER BY last_ts DESC LIMIT 300""", (since,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/presences")
def api_presences():
    """RIDs and TGs seen for the first time within the last N hours --
    new units/agencies appearing on the networks."""
    hours = float(request.args.get("hours", 24))
    if hours not in (1, 6, 24, 72, 168):
        hours = 24
    since = time.time() - hours * 3600
    c = db()
    radios = c.execute("""
        SELECT r.rid, r.alias, r.call_count, r.total_ms, r.first_seen, r.last_seen,
               s.name AS system
          FROM radios r JOIN systems s ON r.system_id=s.id
         WHERE r.first_seen >= ?
         ORDER BY r.first_seen DESC LIMIT 500""", (since,)).fetchall()
    tgs = c.execute("""
        SELECT tg.tgid, tg.alias, tg.tg_group, tg.encrypted,
               tg.call_count, tg.first_seen, tg.last_seen,
               s.name AS system
          FROM talkgroups tg JOIN systems s ON tg.system_id=s.id
         WHERE tg.first_seen >= ?
         ORDER BY tg.first_seen DESC LIMIT 500""", (since,)).fetchall()
    return jsonify({"radios":      [dict(r) for r in radios],
                    "talkgroups":  [dict(r) for r in tgs]})


@app.route("/api/transcripts")
def api_transcripts():
    """All calls that have a transcript, newest first."""
    limit = int(request.args.get("limit", 200))
    only_speech = request.args.get("only_speech", "0") == "1"
    system = request.args.get("system")
    tgid = request.args.get("tgid", type=int)
    q = """SELECT c.id, c.ts, c.tgid, c.source_rid, c.frequency, c.duration_ms,
                  c.encrypted, c.audio_path, c.transcript, c.transcript_engine,
                  c.transcript_lang, c.transcript_ms, c.transcript_confidence,
                  sys.name AS system, tg.alias AS tg_alias, tg.tg_group,
                  r.alias AS rid_alias
             FROM calls c
             LEFT JOIN systems sys   ON c.system_id=sys.id
             LEFT JOIN talkgroups tg ON tg.system_id=c.system_id AND tg.tgid=c.tgid
             LEFT JOIN radios r      ON r.system_id=c.system_id AND r.rid=c.source_rid
            WHERE c.transcribe_state='done'"""
    params = []
    if only_speech:
        # filter Whisper's common silence hallucinations
        q += " AND c.transcript NOT IN ('','.','..','...','Music','Thank you.','Thanks.','Thanks for watching.','Thanks for watching!','I''m sorry.','[BLANK_AUDIO]','[BLANK AUDIO]')"
    if system:
        q += " AND sys.name = ?"
        params.append(system)
    if tgid is not None:
        q += " AND c.tgid = ?"
        params.append(tgid)
    q += " ORDER BY c.ts DESC LIMIT ?"
    params.append(limit)
    return jsonify([dict(r) for r in db().execute(q, params).fetchall()])


@app.route("/api/transcripts/grouped")
def api_transcripts_grouped():
    """Transcripts grouped by system -> talkgroup, newest first within each box,
    boxes and systems sorted by latest activity. Per-TG quota keeps one busy
    talkgroup from starving the others."""
    hours = float(request.args.get("hours", 24))
    per_tg = int(request.args.get("per_tg", 50))
    only_speech = request.args.get("only_speech", "0") == "1"
    since = time.time() - hours * 3600
    q = """SELECT * FROM (
               SELECT c.id, c.ts, c.tgid, c.source_rid, c.frequency, c.duration_ms,
                      c.encrypted, c.audio_path, c.transcript, c.transcript_engine,
                      c.transcript_lang, c.transcript_ms, c.transcript_confidence,
                      c.system_id,
                      sys.name AS system, tg.alias AS tg_alias, tg.tg_group,
                      r.alias AS rid_alias,
                      ROW_NUMBER() OVER (PARTITION BY c.system_id, c.tgid
                                         ORDER BY c.ts DESC) AS rn
                 FROM calls c
                 LEFT JOIN systems sys   ON c.system_id=sys.id
                 LEFT JOIN talkgroups tg ON tg.system_id=c.system_id AND tg.tgid=c.tgid
                 LEFT JOIN radios r      ON r.system_id=c.system_id AND r.rid=c.source_rid
                WHERE c.transcribe_state='done' AND c.ts >= ?"""
    params = [since]
    if only_speech:
        q += " AND c.transcript NOT IN ('','.','..','...','Music','Thank you.','Thanks.','Thanks for watching.','Thanks for watching!','I''m sorry.','[BLANK_AUDIO]','[BLANK AUDIO]')"
    q += ") WHERE rn <= ? ORDER BY ts DESC"
    params.append(per_tg)
    rows = db().execute(q, params).fetchall()

    systems = {}
    for r in rows:
        s = systems.setdefault(r["system"] or "unknown",
                               {"system": r["system"] or "unknown", "n": 0, "tgs": {}})
        s["n"] += 1
        key = (r["system_id"], r["tgid"])
        t = s["tgs"].setdefault(key, {"tgid": r["tgid"], "alias": r["tg_alias"],
                                      "group": r["tg_group"], "n": 0, "items": []})
        t["n"] += 1
        t["items"].append(dict(r))
    out = []
    for s in systems.values():
        # sort boxes by most recent transcript so fresh activity floats to the top
        tgs = sorted(s["tgs"].values(),
                     key=lambda t: -max(i["ts"] for i in t["items"]))
        out.append({"system": s["system"], "n": s["n"], "tgs": tgs})
    out.sort(key=lambda s: -max(i["ts"] for t in s["tgs"] for i in t["items"]))
    return jsonify(out)


def _tuner_label(t):
    """Human name for a tuner entry: 'hackRFTunerConfiguration' -> 'HackRF',
    'rtlSDRTunerConfiguration' -> 'RTL-SDR', etc. Anything SDRTrunk supports."""
    import re as _re
    raw = (t.get("type") or "").replace("TunerConfiguration", "")
    pretty = {"hackrf": "HackRF", "rtlsdr": "RTL-SDR", "airspy": "Airspy",
              "sdrplay": "SDRplay"}.get(raw.lower())
    if pretty:
        return pretty
    # camelCase -> words ('limeSDR' -> 'Lime SDR')
    return _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw).strip().title() or "SDR"


def _tuner_traffic_active(freq, rate_mhz, window_s=600):
    """A tuner counts as present if the event log shows traffic inside its
    receive window (center +/- rate/2) recently -- works on any OS and any
    SDR, no USB tools needed."""
    if not freq or not rate_mhz:
        return None
    half = rate_mhz * 1e6 / 2
    row = db().execute(
        "SELECT 1 FROM events WHERE ts >= ? AND frequency BETWEEN ? AND ? LIMIT 1",
        (time.time() - window_s, freq - half, freq + half)).fetchone()
    return bool(row)


@app.route("/api/tuners")
def api_tuners():
    """Report the SDRTrunk tuner_configuration.json so the dashboard can show
    which SDR is on which band. Presence: USB topology where the OS/tools
    allow it (HackRF on macOS via ioreg/hackrf_info), else recent traffic in
    the tuner's receive window. connected: True present, False absent/silent,
    None when no detection is possible."""
    import json as _json
    import re as _re
    import shutil as _shutil
    import subprocess as _sp
    from pathlib import Path as _Path

    def hackrf_locations():
        """USB topology of plugged-in HackRFs from the IORegistry (macOS only;
        works even while SDRTrunk has the boards claimed, unlike hackrf_info).
        Returns e.g. {'Bus:3 Port:1.2.3'} matching SDRTrunk's uniqueID form."""
        if not _shutil.which("ioreg"):
            return set()
        try:
            out = _sp.run(["ioreg", "-p", "IOUSB", "-w0"],
                          capture_output=True, text=True, timeout=15).stdout
        except Exception:
            return set()
        locs = set()
        for m in _re.finditer(r"HackRF[^@\"]*@([0-9a-fA-F]{8})", out):
            h = m.group(1)
            bus = int(h[:2], 16)
            ports = ".".join(h[2:].rstrip("0"))
            locs.add(f"Bus:{bus} Port:{ports}")
        return locs

    def hackrf_serials():   # fallback when ioreg yields nothing
        if not _shutil.which("hackrf_info"):
            return []
        try:
            out = _sp.run(["hackrf_info"], capture_output=True,
                          text=True, timeout=10).stdout
        except Exception:
            return []
        return _re.findall(r"Serial number:\s*([0-9A-Fa-f]+)", out)

    locs = hackrf_locations()
    serials = [] if locs else hackrf_serials()
    boards_found = len(locs) or len(serials)
    p = _Path(SDRTRUNK_HOME) / "configuration" / "tuner_configuration.json"
    if not p.exists():
        return jsonify({"tuners": [], "boards_found": boards_found, "error": "config not found"})
    try:
        data = _json.loads(p.read_text())
    except Exception as e:
        return jsonify({"tuners": [], "boards_found": boards_found, "error": str(e)})
    tuners = []
    hackrf_seen = 0
    for t in data.get("tunerConfigurations", []):
        is_hackrf = "hackrf" in (t.get("type") or "").lower()
        connected = None
        if is_hackrf and (locs or serials):
            uid = t.get("uniqueID") or ""
            if locs:
                connected = any(loc in uid for loc in locs)
            else:
                # serial-count fallback: can't pair 1:1, cover in config order
                connected = hackrf_seen < len(serials)
        if is_hackrf:
            hackrf_seen += 1
        # Generic presence: recent traffic inside this tuner's receive window.
        # Confirms an idle USB check and covers SDRs with no USB tooling.
        active = _tuner_traffic_active(t.get("frequency"), _rate_mhz(t.get("sampleRate")))
        if connected is not True:
            connected = True if active else (connected if connected is not None
                                             else (False if active is not None else None))
        tuners.append({
            "type": t.get("type"),
            "label": _tuner_label(t),
            "uniqueID": t.get("uniqueID"),
            "sampleRate": t.get("sampleRate"),
            "frequency": t.get("frequency"),
            "amplifierEnabled": t.get("amplifierEnabled"),
            "lnagain": t.get("lnagain"),
            "vgagain": t.get("vgagain"),
            "connected": connected,
        })
    return jsonify({"tuners": tuners, "boards_found": boards_found})


def _rate_mhz(s):
    # "RATE_10_0" -> 10.0
    try:
        return float((s or "").replace("RATE_", "").replace("_", "."))
    except ValueError:
        return 0.0


@app.route("/api/coverage")
def api_coverage():
    """Which coverage targets sit inside a configured tuner's receive window
    (center +/- sample_rate/2). Targets live in config/coverage_targets.json
    and are the import seam for a future RadioReference pull."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(SDRTRUNK_HOME) / "configuration" / "tuner_configuration.json"
    try:
        data = _json.loads(p.read_text())
    except Exception:
        data = {}
    windows = []
    for t in data.get("tunerConfigurations", []):
        f, r = t.get("frequency"), _rate_mhz(t.get("sampleRate"))
        if f and r:
            half = r * 1e6 / 2
            windows.append({"uid": t.get("uniqueID"), "lo": f - half, "hi": f + half})

    tp = _Path(__file__).parent / "config" / "coverage_targets.json"
    targets = _json.loads(tp.read_text()).get("targets", []) if tp.exists() else []
    out = []
    for t in targets:
        freqs_hz = [f * 1e6 for f in t.get("freqs", [])]
        covered_by = sorted({w["uid"] for f in freqs_hz for w in windows
                             if w["lo"] <= f <= w["hi"]})
        out.append({
            "name": t["name"], "kind": t.get("kind"), "status": t.get("status"),
            "freqs": t.get("freqs", []),
            "covered": bool(covered_by),
            "covered_by": covered_by,
        })
    return jsonify({"windows": [{"uid": w["uid"],
                                 "center_mhz": round((w["lo"] + w["hi"]) / 2 / 1e6, 5),
                                 "lo_mhz": round(w["lo"] / 1e6, 5),
                                 "hi_mhz": round(w["hi"] / 1e6, 5)} for w in windows],
                    "targets": out})


@app.route("/api/tg/<int:tgid>")
def api_tg_profile(tgid):
    """Full profile for one talkgroup: calls, top RIDs, encryption %, sites."""
    c = db()
    info = c.execute("""SELECT tg.tgid, tg.alias, tg.tg_group, tg.priority, tg.encrypted,
                               tg.call_count, tg.total_ms, sys.name AS system,
                               tg.first_seen, tg.last_seen
                          FROM talkgroups tg JOIN systems sys ON tg.system_id=sys.id
                         WHERE tg.tgid=?""", (tgid,)).fetchone()
    if not info:
        return jsonify({"error": "no such tg"}), 404
    calls = c.execute("""SELECT ts, source_rid, encrypted, frequency, duration_ms, transcript
                           FROM calls WHERE tgid=? ORDER BY ts DESC LIMIT 200""",
                      (tgid,)).fetchall()
    top_rids = c.execute("""SELECT source_rid AS rid, COUNT(*) AS n
                              FROM calls WHERE tgid=? AND source_rid IS NOT NULL
                          GROUP BY source_rid ORDER BY n DESC LIMIT 20""",
                         (tgid,)).fetchall()
    enc_ratio = c.execute("""SELECT
                               SUM(CASE WHEN encrypted=1 THEN 1 ELSE 0 END) AS enc,
                               COUNT(*) AS total FROM calls WHERE tgid=?""", (tgid,)).fetchone()
    return jsonify({
        "info": dict(info),
        "calls": [dict(r) for r in calls],
        "top_rids": [dict(r) for r in top_rids],
        "enc_ratio": dict(enc_ratio) if enc_ratio else {},
    })


# ---- boot --------------------------------------------------------------------
def _ensure_single_instance(host, port):
    """Kill any prior dashboard on this port before binding. Without this,
    SO_REUSEPORT lets multiple workers bind the same port and round-robin
    connections between stale versions."""
    import socket, subprocess
    # 1. Kill any other Python process holding app.py with same port
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", f"app.py --host {host} --port {port}"],
            text=True)
        my_pid = os.getpid()
        for pid in [int(x) for x in out.split() if x.strip().isdigit()]:
            if pid != my_pid:
                print(f"[boot] killing stale instance pid={pid}")
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
    except Exception:
        # pgrep exits 1 on no match; under eventlet monkey_patch its
        # CalledProcessError is a different class object, so catch broadly
        pass
    # 2. Confirm the port is now free
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            s.close()
            return
        except OSError:
            time.sleep(0.2)
        finally:
            s.close()
    print(f"[boot] port {port} still busy after killing stale instances", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5544)
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR,
                    help="SDRTrunk event_logs directory to tail")
    ap.add_argument("--no-whisper", action="store_true")
    ap.add_argument("--no-tail",    action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="Wipe DB and audio_calls/ on startup")
    args = ap.parse_args()

    _ensure_single_instance(args.host, args.port)

    if args.fresh:
        print("[boot] fresh start: wiping DB and audio_calls/")
        db_wipe()
        from ingest.rdio import AUDIO_DIR
        for p in AUDIO_DIR.glob("**/*"):
            if p.is_file():
                p.unlink()
    init_db()

    # Auto-import aliases if none exist
    if db().execute("SELECT COUNT(*) FROM talkgroups").fetchone()[0] == 0:
        r = aliases_mod.import_from()
        print(f"[boot] aliases imported: {r}")

    if not args.no_tail and Path(args.log_dir).exists():
        print(f"[boot] tailing {args.log_dir}")
        start_tail(args.log_dir, socketio=socketio)
    elif not args.no_tail:
        print(f"[boot] log dir missing, skipping tail: {args.log_dir}")

    if not args.no_whisper:
        start_whisper(socketio=socketio)

    print(f"[boot] listening on http://{args.host}:{args.port}")
    socketio.run(app, host=args.host, port=args.port,
                 debug=False, use_reloader=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
