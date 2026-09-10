-- trunk-tap schema
-- Captures every trunking event and every completed call SDRTrunk streams us.
-- Encrypted calls still yield RID, TG, site, freq, duration, timestamp.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- systems: one row per network ("Countywide", "State P25 Site 700", ...).
-- Site-specific SDRTrunk log names are canonicalized at ingest (rules in
-- config/systems.json); sites/towers live in the sites table.
CREATE TABLE IF NOT EXISTS systems (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT UNIQUE NOT NULL,
    protocol     TEXT,              -- APCO-25, NBFM, ...
    label        TEXT,              -- human label as seen in SDRTrunk
    first_seen   REAL,
    last_seen    REAL
);

-- sites: one row per RFSS site or channel-plan tower
CREATE TABLE IF NOT EXISTS sites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    system_id    INTEGER REFERENCES systems(id),
    name         TEXT,              -- "State P25 Site 700"
    site_id      TEXT,              -- "1-1" or "006"
    rfss         INTEGER,
    wacn         INTEGER,
    lat          REAL,
    lon          REAL,
    first_seen   REAL,
    last_seen    REAL,
    UNIQUE(system_id, site_id)
);

-- talkgroups
CREATE TABLE IF NOT EXISTS talkgroups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    system_id    INTEGER REFERENCES systems(id),
    tgid         INTEGER NOT NULL,
    alias        TEXT,              -- "County PD Dispatch"
    tg_group     TEXT,              -- "Law Enforcement"
    tg_tag       TEXT,              -- category
    priority     INTEGER,
    encrypted    INTEGER DEFAULT 0, -- 1 if ever seen encrypted
    call_count   INTEGER DEFAULT 0,
    total_ms     INTEGER DEFAULT 0,
    first_seen   REAL,
    last_seen    REAL,
    UNIQUE(system_id, tgid)
);

-- radios (RIDs / source IDs)
CREATE TABLE IF NOT EXISTS radios (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    system_id    INTEGER REFERENCES systems(id),
    rid          INTEGER NOT NULL,
    alias        TEXT,
    call_count   INTEGER DEFAULT 0,
    total_ms    INTEGER DEFAULT 0,
    first_seen   REAL,
    last_seen    REAL,
    UNIQUE(system_id, rid)
);

-- completed calls (from RDIO Scanner protocol POSTs, includes audio ref)
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    system_id     INTEGER REFERENCES systems(id),
    site_id       INTEGER REFERENCES sites(id),
    talkgroup_id  INTEGER REFERENCES talkgroups(id),
    tgid          INTEGER,
    source_rid    INTEGER,
    sources_json  TEXT,               -- all RIDs that transmitted
    frequency     INTEGER,
    frequencies_json TEXT,             -- freq hops during call
    duration_ms   INTEGER,
    encrypted     INTEGER DEFAULT 0,
    patches_json  TEXT,
    audio_path    TEXT,                -- path under audio_calls/
    audio_type    TEXT,                -- mp3, wav
    audio_bytes   INTEGER,
    raw_json      TEXT,                -- full payload for forensics
    transcript          TEXT,          -- Whisper output (NULL until transcribed)
    transcript_engine   TEXT,          -- "mlx-whisper", "whisper.cpp", ...
    transcript_model    TEXT,          -- "large-v3-turbo", etc.
    transcript_lang     TEXT,
    transcript_ms       INTEGER,       -- wall time to transcribe
    transcript_at       REAL,
    transcript_confidence REAL,
    transcribe_state    TEXT DEFAULT 'pending'   -- pending | done | skipped | failed
);
CREATE INDEX IF NOT EXISTS idx_calls_ts        ON calls(ts DESC);
CREATE INDEX IF NOT EXISTS idx_calls_tgid      ON calls(system_id, tgid);
CREATE INDEX IF NOT EXISTS idx_calls_rid       ON calls(system_id, source_rid);
CREATE INDEX IF NOT EXISTS idx_calls_site      ON calls(site_id);
CREATE INDEX IF NOT EXISTS idx_calls_encrypted ON calls(encrypted);
CREATE INDEX IF NOT EXISTS idx_calls_transcribe ON calls(transcribe_state, ts);
-- Plain (non-contentless) FTS5 so we can UPDATE/DELETE freely.
CREATE VIRTUAL TABLE IF NOT EXISTS calls_fts USING fts5(
    transcript, tg_label, tg_group, system_label
);

-- generic trunking events (from CSV tail: register, response, group call grant,
-- unit call, data call, patch, adjacent site, denied grant, etc.)
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    system_id     INTEGER REFERENCES systems(id),
    site_id       INTEGER REFERENCES sites(id),
    protocol      TEXT,
    event_type    TEXT,               -- Register, Response, Group Call, Unit Call, Data Call, Patch, Deny, ...
    from_rid      INTEGER,
    to_tgid       INTEGER,
    to_rid        INTEGER,
    channel       TEXT,
    frequency     INTEGER,
    timeslot      INTEGER,
    duration_ms   INTEGER,
    details       TEXT,
    event_id      TEXT,               -- SDRTrunk's event_id
    encrypted     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_sys    ON events(system_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_from   ON events(from_rid);
CREATE INDEX IF NOT EXISTS idx_events_to_tg  ON events(to_tgid);
CREATE INDEX IF NOT EXISTS idx_events_type   ON events(event_type);
-- SDRTrunk emits a unique event_id per row; use it as a dedup key so re-scan
-- (from FS-watcher + poll racing) doesn't double-insert.
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_evid ON events(system_id, event_id)
    WHERE event_id IS NOT NULL;

-- affiliations: which TG a RID is registered to (from Response ACCEPTED AFFILIATION)
CREATE TABLE IF NOT EXISTS affiliations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    system_id    INTEGER REFERENCES systems(id),
    site_id      INTEGER REFERENCES sites(id),
    rid          INTEGER NOT NULL,
    tgid         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aff_rid ON affiliations(rid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_aff_tg  ON affiliations(tgid, ts DESC);

-- roaming: RID observed at which site over time
CREATE TABLE IF NOT EXISTS roaming (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    system_id    INTEGER REFERENCES systems(id),
    site_id      INTEGER REFERENCES sites(id),
    rid          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roam_rid  ON roaming(rid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_roam_site ON roaming(site_id, ts DESC);

-- patches (TG patched to TG)
CREATE TABLE IF NOT EXISTS patches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    system_id    INTEGER REFERENCES systems(id),
    supergroup   INTEGER,
    child_tgs    TEXT             -- JSON array of TGIDs
);

-- denied grants (censored / busy / DENY events)
CREATE TABLE IF NOT EXISTS denies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    system_id    INTEGER REFERENCES systems(id),
    site_id      INTEGER REFERENCES sites(id),
    rid          INTEGER,
    tgid         INTEGER,
    reason       TEXT
);

-- adjacent-site broadcasts (network topology inference)
CREATE TABLE IF NOT EXISTS adjacent_sites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    system_id    INTEGER REFERENCES systems(id),
    from_site_id INTEGER REFERENCES sites(id),
    neighbor_site TEXT,           -- neighbor RFSS/site id string
    frequency    INTEGER
);

-- anomalies: novel behavior worth alerting on (new RID, new TG, spike, roam)
CREATE TABLE IF NOT EXISTS anomalies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,      -- new_rid, new_tg, new_site, new_rid_on_tg,
                                     -- new_rid_at_site, spike, enc_change
    system_id    INTEGER REFERENCES systems(id),
    site_id      INTEGER REFERENCES sites(id),
    rid          INTEGER,
    tgid         INTEGER,
    details      TEXT,
    ack          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_anom_ts   ON anomalies(ts DESC);
CREATE INDEX IF NOT EXISTS idx_anom_kind ON anomalies(kind, ts DESC);

-- ingest log (bookkeeping for tailed files)
CREATE TABLE IF NOT EXISTS ingest_state (
    path         TEXT PRIMARY KEY,
    offset       INTEGER NOT NULL DEFAULT 0,
    last_ts      REAL
);
