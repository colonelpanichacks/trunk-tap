"""
Whisper transcription worker.

Polls the calls table for rows with transcribe_state='pending' and a real audio
file, runs Whisper locally, writes transcript back, updates FTS, emits over WS.

Backends (auto-selected in order):
  1. mlx-whisper (Apple Silicon, fastest) -- pip install mlx-whisper
  2. whisper.cpp binary at $WHISPER_CPP_BIN with $WHISPER_CPP_MODEL
  3. openai/whisper python package (slow CPU fallback)

The whole worker is a no-op if none of the above are available -- calls just
sit in 'pending' until you install one.
"""
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from db import db

# audio_path in the DB is relative ("audio_calls/...") -- resolve it against
# the data dir (SDRTD_DATA_DIR), same root ingest/rdio.py writes to.
AUDIO_ROOT = Path(os.environ.get("SDRTD_DATA_DIR", Path(__file__).resolve().parents[1]))

MODEL = os.environ.get("SDRTD_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
CPP_BIN = os.environ.get("WHISPER_CPP_BIN")
CPP_MODEL = os.environ.get("WHISPER_CPP_MODEL")

# Radio calls are short, noisy, accented clips. The initial prompt is written as
# *sample utterances* in dispatch style, not a description -- a descriptive
# prompt ("...police radio traffic") gets parroted back verbatim when the audio
# is unintelligible, which is exactly the hallucination we're killing. Pinning
# English stops accent-driven language misdetection, and each call is an
# independent clip so no conditioning on previous text. Override with
# SDRTD_WHISPER_PROMPT.
PROMPT = os.environ.get("SDRTD_WHISPER_PROMPT",
    "Dispatch, unit 214, 10-76 en route to 245 Main Street. "
    "Engine 3 is 10-97 on scene. 10-4, standby for the battalion chief. "
    "Can I get a 10-28 on a gray Ford pickup, out-of-state tag?")

# Whisper's classic noise/silence hallucinations. If the whole transcript
# normalizes to one of these, store an empty transcript instead of a guess.
HALLUCINATIONS = {
    "thank you", "thank you.", "thanks", "thanks.", "thank you very much",
    "thanks for watching", "thanks for watching.", "thanks for watching!",
    "music", ".", "..", "...", "you", "bye", "bye.", "i'm sorry", "i'm sorry.",
    "[blank_audio]", "[blank audio]", "(static)", "(silence)", "(radio static)",
    "police traffic", "radio traffic", "police radio traffic",
    "fire, ems, sheriff, police radio traffic",
}

_HALLUC_RE = re.compile(r"[^a-z0-9 ]+")


def _norm(s):
    return _HALLUC_RE.sub("", (s or "").lower()).strip()


_HALLUC_NORM = {_norm(h) for h in HALLUCINATIONS}
_PROMPT_TOKENS = _norm(PROMPT).split()

# Historical transcripts came from the old descriptive prompt; whisper parrots
# it with drift ("Air, EMS..." for "Fire, EMS..."). Prefix-match those.
_LEGACY_PARROTS = (
    "air ems sheriff police radio traffic",
    "fire ems sheriff police radio traffic",
    "public safety twoway radio dispatch traffic",
)


def _lcs_len(a, b):
    """Length of longest common subsequence of two token lists (both tiny)."""
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[-1]))
        prev = cur
    return prev[-1]


def _is_hallucination(text, max_cr=None):
    """Detect Whisper's noise hallucinations. Three catches:
    1. classic canned phrases (normalized exact match)
    2. prompt echo -- most of the transcript (>=8 tokens, >=80% of it) is a
       reordered/near-verbatim chunk of our own initial_prompt; whisper
       parrots the prompt onto unintelligible audio
    3. repetition loop -- worst segment compression ratio above 2.4, or (when
       no segment data exists) one word making up >=70% of a >=10-token text"""
    norm = _norm(text)
    if not norm or norm in _HALLUC_NORM:
        return True
    if norm.startswith(_LEGACY_PARROTS):
        return True
    if max_cr is not None and max_cr > 2.4:
        return True
    tokens = norm.split()
    if len(tokens) >= 10:
        from collections import Counter
        words = [t for t in tokens if len(t) > 1]
        if words:
            word, n = Counter(words).most_common(1)[0]
            if n / len(tokens) >= 0.7:
                return True
    if len(tokens) >= 8:
        lcs = _lcs_len(tokens, _PROMPT_TOKENS)
        if lcs >= 8 and lcs / len(tokens) >= 0.8:
            return True
    return False



def _mlx_available():
    try:
        import mlx_whisper  # noqa
        return True
    except Exception:
        return False


def _whisper_py_available():
    try:
        import whisper  # noqa
        return True
    except Exception:
        return False


def _cpp_available():
    return bool(CPP_BIN and CPP_MODEL and shutil.which(CPP_BIN) and Path(CPP_MODEL).exists())


def _transcribe_mlx(audio_path):
    import mlx_whisper
    t0 = time.time()
    result = mlx_whisper.transcribe(str(audio_path), path_or_hf_repo=MODEL,
                                    language="en", initial_prompt=PROMPT,
                                    condition_on_previous_text=False,
                                    temperature=0.0)
    ms = int((time.time() - t0) * 1000)
    text = (result.get("text") or "").strip()
    lang = result.get("language")
    # confidence: average of segment probs if provided; also grab the worst
    # compression ratio -- >2.4 marks a repetition loop (whisper thresholds
    # only work with temperature fallback, so we police it ourselves)
    conf = None
    max_cr = None
    segs = result.get("segments") or []
    if segs:
        probs = [s.get("avg_logprob") for s in segs if s.get("avg_logprob") is not None]
        if probs:
            conf = sum(probs) / len(probs)
        crs = [s.get("compression_ratio") for s in segs if s.get("compression_ratio") is not None]
        if crs:
            max_cr = max(crs)
    return {"text": text, "lang": lang, "engine": "mlx-whisper",
            "model": MODEL, "ms": ms, "conf": conf, "max_cr": max_cr}


def _transcribe_cpp(audio_path):
    t0 = time.time()
    out = subprocess.run(
        [CPP_BIN, "-m", CPP_MODEL, "-f", str(audio_path), "-nt",
         "-l", "en", "--prompt", PROMPT],
        capture_output=True, text=True, timeout=180)
    ms = int((time.time() - t0) * 1000)
    text = (out.stdout or "").strip()
    return {"text": text, "lang": None, "engine": "whisper.cpp",
            "model": Path(CPP_MODEL).name, "ms": ms, "conf": None}


def _transcribe_py(audio_path):
    import whisper
    t0 = time.time()
    model = whisper.load_model(os.environ.get("SDRTD_WHISPER_PY_MODEL", "base"))
    result = model.transcribe(str(audio_path), language="en",
                              initial_prompt=PROMPT,
                              condition_on_previous_text=False,
                              temperature=0.0)
    ms = int((time.time() - t0) * 1000)
    return {"text": (result.get("text") or "").strip(), "lang": result.get("language"),
            "engine": "openai-whisper", "model": model.dims.n_text_ctx and "base" or "base",
            "ms": ms, "conf": None}


def _pick_engine():
    if _mlx_available():
        return _transcribe_mlx, "mlx-whisper"
    if _cpp_available():
        return _transcribe_cpp, "whisper.cpp"
    if _whisper_py_available():
        return _transcribe_py, "openai-whisper"
    return None, None


def _audio_duration_ms(path):
    """Duration of an audio file via macOS afinfo (no extra deps). None if unknown."""
    try:
        out = subprocess.run(["afinfo", str(path)], capture_output=True,
                             text=True, timeout=10).stdout
        m = re.search(r"estimated duration:\s*([\d.]+)", out)
        if m:
            return int(float(m.group(1)) * 1000)
    except Exception:
        pass
    return None


def _apply_duration(c, call, dur):
    """Set calls.duration_ms and roll it into the talkgroup/radio air-time
    totals. Guarded so it only ever applies once per call (subsequent runs
    see duration_ms already set)."""
    n = c.execute("UPDATE calls SET duration_ms=? WHERE id=? AND duration_ms IS NULL",
                  (dur, call["id"])).rowcount
    if not n:
        return
    c.execute("UPDATE talkgroups SET total_ms=total_ms+? WHERE system_id=? AND tgid=?",
              (dur, call["system_id"], call["tgid"]))
    if call["source_rid"] is not None:
        c.execute("UPDATE radios SET total_ms=total_ms+? WHERE system_id=? AND rid=?",
                  (dur, call["system_id"], call["source_rid"]))


def backfill_duration(c, call, audio):
    """Set calls.duration_ms from the audio file."""
    dur = _audio_duration_ms(audio)
    if dur:
        _apply_duration(c, call, dur)


def backfill_duration_from_events(c, call):
    """Encrypted calls carry no audio, so RDIO uploads arrive with no duration
    and no file to recover it from. The control channel still logged the grant
    though -- match on system/talkgroup/radio at the nearest timestamp and take
    the grant's duration. This is what gives encrypted traffic air-time stats."""
    q = """SELECT duration_ms, ABS(ts - ?) AS dt
             FROM events
            WHERE system_id=? AND to_tgid=? AND duration_ms IS NOT NULL"""
    params = [call["ts"], call["system_id"], call["tgid"]]
    if call["source_rid"] is not None:
        q += " AND from_rid=?"
        params.append(call["source_rid"])
    q += " AND ts BETWEEN ? AND ? ORDER BY dt LIMIT 1"
    params += [call["ts"] - 30, call["ts"] + 30]
    row = c.execute(q, params).fetchone()
    if row:
        _apply_duration(c, call, row["duration_ms"])


def backfill_durations_from_events(c, limit=25):
    """Idle-time sweep for any call still missing a duration."""
    rows = c.execute(
        """SELECT id, ts, system_id, tgid, source_rid
             FROM calls WHERE duration_ms IS NULL
            ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    for call in rows:
        backfill_duration_from_events(c, call)


def _worker_loop(socketio=None, idle_sleep=1.5):
    fn, name = _pick_engine()
    if fn is None:
        print("[whisper] no backend available -- install mlx-whisper, whisper.cpp, or openai-whisper")
        return
    model_desc = {"mlx-whisper": MODEL, "whisper.cpp": CPP_MODEL,
                  "openai-whisper": os.environ.get("SDRTD_WHISPER_PY_MODEL", "base")}.get(name, MODEL)
    print(f"[whisper] worker started backend={name} model={model_desc}")
    while True:
        c = db()
        row = c.execute(
            """SELECT c.id, c.audio_path, c.ts, c.tgid, c.source_rid,
                      c.frequency, c.encrypted, c.system_id,
                      s.name AS system, tg.alias AS tg_alias
                 FROM calls c
                 LEFT JOIN systems s ON s.id=c.system_id
                 LEFT JOIN talkgroups tg ON tg.system_id=c.system_id AND tg.tgid=c.tgid
                WHERE c.transcribe_state='pending' AND c.audio_path IS NOT NULL
                ORDER BY c.ts ASC LIMIT 1""").fetchone()
        if row is None:
            # idle: recover durations for encrypted/audio-less calls from
            # control-channel grants
            try:
                backfill_durations_from_events(c)
            except Exception as e:
                print(f"[whisper] event duration backfill err: {e!r}")
            time.sleep(idle_sleep)
            continue

        call_id = row["id"]
        rel = row["audio_path"]
        audio = AUDIO_ROOT / rel
        if not audio.exists():
            c.execute("UPDATE calls SET transcribe_state='skipped' WHERE id=?", (call_id,))
            continue

        # RDIO uploads carry no duration -- recover it from the audio file so
        # AIR-time columns/stats work. One-shot per call (guarded inside).
        try:
            backfill_duration(c, row, audio)
        except Exception as e:
            print(f"[whisper] duration backfill err call={call_id}: {e!r}")

        try:
            r = fn(audio)
            if _is_hallucination(r["text"], r.get("max_cr")):
                # unintelligible clip -- store empty rather than a confident guess
                r["text"] = ""
            c.execute(
                """UPDATE calls SET transcript=?, transcript_engine=?, transcript_model=?,
                                    transcript_lang=?, transcript_ms=?, transcript_at=?,
                                    transcript_confidence=?, transcribe_state='done'
                     WHERE id=?""",
                (r["text"], r["engine"], r["model"], r["lang"], r["ms"], time.time(),
                 r["conf"], call_id))
            # Refresh FTS row with the transcript
            c.execute("UPDATE calls_fts SET transcript=? WHERE rowid=?",
                      (r["text"], call_id))
            # Drop a .txt next to the per-system export tree on the Desktop
            try:
                from ingest.tail import write_transcript_file
                write_transcript_file(
                    row["system"] or "unknown", row["ts"], row["tgid"],
                    row["tg_alias"], row["source_rid"], call_id, r["text"],
                    engine=r["engine"], model=r["model"], lang=r["lang"],
                    ms=r["ms"], conf=r["conf"], audio_path=rel,
                    frequency=row["frequency"], encrypted=bool(row["encrypted"]))
            except Exception as e:
                print(f"[whisper] transcript file err call={call_id}: {e!r}")
            if socketio is not None:
                socketio.emit("transcript", {
                    "call_id": call_id,
                    "text": r["text"],
                    "engine": r["engine"],
                    "lang": r["lang"],
                    "ms": r["ms"],
                })
        except Exception as e:
            import traceback
            print(f"[whisper] fail call={call_id} err={e!r}", flush=True)
            traceback.print_exc()
            c.execute("UPDATE calls SET transcribe_state='failed' WHERE id=?", (call_id,))


def start_worker(socketio=None):
    t = threading.Thread(target=_worker_loop, args=(socketio,),
                         name="whisper-worker", daemon=True)
    t.start()
    return t
