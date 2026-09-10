// trunk-tap front end
(() => {
  const $ = (s, r=document) => r.querySelector(s);
  const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));

  const fmtTs = ts => {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0,8);
  };
  const fmtDur = ms => ms ? (ms/1000).toFixed(1)+"s" : "--";
  const fmtAgo = ts => {
    if (!ts) return "--";
    const s = Math.max(0, Date.now()/1000 - ts);
    return s < 60 ? "<1m ago" : s < 3600 ? Math.floor(s/60)+"m ago"
         : s < 86400 ? Math.floor(s/3600)+"h ago" : Math.floor(s/86400)+"d ago";
  };
  // "new presence" = first seen on the network within the last 24h
  const isNew = ts => ts && (Date.now()/1000 - ts) < 86400;
  const newBadge = ts => isNew(ts) ? ` <span class="badge-new">NEW</span>` : "";
  const esc = s => (s ?? "").toString()
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

  // ---- RadioReference deep links ----
  // RR has per-SYSTEM pages only (no per-TG or per-RID pages), so every link
  // goes to the system's page. RR_SIDS + CANON_RULES come from /api/config
  // (config/systems.json); canonSys applies the same rules as
  // canonical_system() in db/__init__.py because live WS calls carry the raw
  // SDRTrunk label.
  const RR_SIDS = {};
  let CANON_RULES = [];
  const canonSys = s => {
    if (!s) return s;
    for (const r of CANON_RULES) {
      if (r.exact && r.exact[s]) return r.exact[s];
      if (r.prefix && s.startsWith(r.prefix)) return r.canonical || s;
    }
    return s;
  };
  const rrUrl = system => {
    const sid = RR_SIDS[canonSys(system)];
    return sid ? `https://www.radioreference.com/db/sid/${sid}` : null;
  };
  const rrLink = (system, label) => {
    const text = esc(label);
    if (!text) return "";
    const u = rrUrl(system);
    return u ? `<a class="rr" href="${u}" target="_blank" rel="noopener" title="View on RadioReference">${text}</a>` : text;
  };

  // ---- tabs ----
  $$("nav .tab").forEach(b => b.addEventListener("click", () => {
    $$("nav .tab").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    $$(".tab-panel").forEach(x => x.classList.remove("active"));
    $("#tab-"+b.dataset.tab).classList.add("active");
    history.replaceState(null, "", "#"+b.dataset.tab);
    onTabShown(b.dataset.tab);
  }));
  // deep-link: open the tab named in the URL hash, e.g. /#topology
  // (deferred: onTabShown is declared later in this scope)
  const hashTab = location.hash.slice(1);
  if (hashTab && $("#tab-"+hashTab)) {
    setTimeout(() => $(`nav .tab[data-tab="${hashTab}"]`)?.click(), 0);
  }

  const liveEvents = $("#live-events");
  const MAX_LIVE   = 200;

  // Color-code control-channel rows by event type (OSINT view of the network)
  const EVT_CLASS = t =>
    /deny/i.test(t)     ? "ev-deny"  :
    /encrypt/i.test(t)  ? "ev-enc"   :
    /patch/i.test(t)    ? "ev-patch" :
    /register/i.test(t) ? "ev-reg"   :
    /call/i.test(t)     ? "ev-voice" : "ev-other";

  const topoLink = label =>
    ` <span class="topo-link" data-topo="${esc(label)}" title="show in topology">topo</span>`;
  const evtRow = e => {
    // API rows use from_rid/to_tgid/event_type; WS events use from/to_tg/type
    const type    = e.event_type || e.type || "";
    const fromRid = e.from_rid ?? e.from;
    const toTg    = e.to_tgid ?? e.to_tg;
    const toRid   = e.to_rid;
    const f = e.frequency || e.freq;
    const fromCell = fromRid ? `<span class="click-rid" data-rid="${fromRid}">RID ${fromRid}</span>${topoLink("RID "+fromRid)}` : "";
    const toCell = toTg  ? `<span class="click-tg" data-tg="${toTg}">TG ${toTg}</span>${topoLink("TG "+toTg)}`
                 : toRid ? `<span class="click-rid" data-rid="${toRid}">RID ${toRid}</span>${topoLink("RID "+toRid)}` : "";
    return `<tr class="${e.encrypted?"enc":""} ${EVT_CLASS(type)}">
      <td class="ts">${fmtTs(e.ts)}</td>
      <td class="sys">${esc(canonSys(e.system)||"")}</td>
      <td class="type">${esc(type)}</td>
      <td>${fromCell}</td>
      <td>${toCell}</td>
      <td class="freq">${f ? fmtFreq(f) : ""}</td>
      <td class="d">${esc(e.details||"")}</td>
    </tr>`;
  };

  const prepend = (parent, html) => {
    parent.insertAdjacentHTML("afterbegin", html);
    while (parent.childElementCount > MAX_LIVE) parent.lastElementChild.remove();
  };

  // ---- ws ----
  const socket = io({transports:["websocket","polling"]});
  const wsState = $("#ws-state");
  socket.on("connect",    () => { wsState.textContent="connected"; wsState.className="conn"; });
  socket.on("disconnect", () => { wsState.textContent="disconnected"; wsState.className="disc"; });

  // ---- single-feed monitor ("zoom in on one feed's audio") ----
  // LISTEN on any call row pins that talkgroup to the bar above the footer;
  // every new completed call on it autoplays. STOP clears.
  let monitorTg = null, monitorCallId = null;
  const monBar  = $("#monitor"), monAudio = $("#mon-audio"),
        monName = $("#mon-name"), monNote = $("#mon-note"),
        monTx   = $("#mon-tx");
  // freq readout next to the TG name (inserted here so no template change is needed)
  const monFreq = document.createElement("span");
  monFreq.id = "mon-freq";
  monName.after(monFreq);
  const setMonitor = (tgid, name, freq) => {
    monitorTg = String(tgid);
    monitorCallId = null;
    monName.textContent = name;
    monFreq.textContent = freq ? fmtFreq(freq) : "";
    monNote.textContent = "waiting for next transmission...";
    monTx.textContent = "";
    monBar.classList.remove("hidden");
  };
  const clearMonitor = () => {
    monitorTg = null;
    monitorCallId = null;
    monAudio.pause(); monAudio.removeAttribute("src"); monAudio.load();
    monBar.classList.add("hidden");
  };
  $("#mon-stop")?.addEventListener("click", clearMonitor);

  // ---- STREAM tab: near-live audio of every finished call, FIFO queue ----
  // One reused <audio> element; START is the user gesture that unlocks sound.
  const streamAudio = $("#stream-audio");
  const STREAM_MAXQ = 10, STREAM_MAXLOG = 100;
  let streaming = false, streamQueue = [], streamNowCall = null;
  let streamPlayed = 0, streamDropped = 0, streamEnc = 0;
  // WS calls carry only the raw tg_label, so aliases come from /api/talkgroups
  let tgAliasMap = null;   // "system|tgid" -> alias
  const loadTgAliasMap = async () => {
    if (tgAliasMap) return;
    const tgs = await fetch("/api/talkgroups?limit=800").then(r=>r.json());
    tgAliasMap = {};
    for (const t of tgs) tgAliasMap[`${t.system}|${t.tgid}`] = t.alias || "";
  };
  const streamTgName = c =>
    tgAliasMap?.[`${canonSys(c.system)}|${c.tgid}`] || c.tg_label || "";

  // include-list filters: comma-separated; numeric term = exact id,
  // anything else = case-insensitive substring of alias/name
  const streamTerms = id => ($("#"+id)?.value || "").split(",")
      .map(s=>s.trim().toLowerCase()).filter(Boolean);
  const streamMatch = (terms, id, text) => !terms.length || terms.some(t =>
    /^\d+$/.test(t) ? String(id ?? "") === t : (text||"").toLowerCase().includes(t));
  const streamPasses = c =>
    streamMatch(streamTerms("stream-f-sys"), null, canonSys(c.system)) &&
    streamMatch(streamTerms("stream-f-tg"), c.tgid, streamTgName(c)) &&
    streamMatch(streamTerms("stream-f-rid"), c.source, c.rid_alias);

  const streamCounts = () => {
    $("#stream-counts").textContent =
      `played ${streamPlayed} · skipped enc ${streamEnc} · dropped ${streamDropped}`;
  };
  const renderStreamNow = () => {
    const el = $("#stream-now");
    if (!streamNowCall) { el.textContent = streaming ? "waiting for traffic..." : "idle"; return; }
    const c = streamNowCall, name = streamTgName(c);
    el.innerHTML = `<span style="color:var(--neon-lime);font-size:10px;letter-spacing:2px">NOW PLAYING</span><br>
      ${fmtTs(c.ts)} · ${rrLink(c.system, name || "TG "+c.tgid)} <span style="color:var(--dim)">(${c.tgid})</span><br>
      <span class="click-rid" data-rid="${c.source||""}">${esc(c.rid_alias||"") || "RID "+(c.source ?? "?")}</span>
      · <span style="color:var(--dim)">${fmtFreq(c.frequency)}</span>
      · <span style="color:var(--dim)">${esc(c.system||"")}</span>`;
  };
  const renderStreamNext = () => {
    const el = $("#stream-next");
    el.textContent = streamQueue.length
      ? `up next (${streamQueue.length}): ` +
        streamQueue.slice(0,5).map(c => streamTgName(c) || "TG "+c.tgid).join(" · ") +
        (streamQueue.length > 5 ? " ..." : "")
      : "";
  };
  const streamLogRow = c => {
    const log = $("#stream-log");
    log.insertAdjacentHTML("afterbegin", `<div class="tx-item">
      <span class="ts">${fmtTs(c.ts)}</span>
      <span class="rid click-tg" data-tg="${c.tgid||""}">${esc(streamTgName(c) || "TG "+c.tgid)}</span>
      <span class="rid click-rid" data-rid="${c.source||""}">${esc(c.rid_alias||"") || "RID "+(c.source ?? "?")}</span>
      <span class="tx">${fmtFreq(c.frequency)}</span>
    </div>`);
    while (log.childElementCount > STREAM_MAXLOG) log.lastElementChild.remove();
    $("#stream-log-n").textContent = streamPlayed;
  };
  const streamPlayNext = () => {
    const c = streamQueue.shift();
    if (!c) { streamNowCall = null; renderStreamNow(); return; }
    streamNowCall = c;
    streamPlayed++;
    renderStreamNow();
    renderStreamNext();
    streamCounts();
    streamLogRow(c);
    streamAudio.src = "/api/audio/" + c.audio;
    streamAudio.play().catch(() => { $("#stream-next").textContent = "click START again to allow sound"; });
  };
  const streamEnqueue = c => {
    if (!streaming) return;
    // encrypted calls have no usable audio -- count them so listeners know
    if (c.encrypted || !c.audio) { streamEnc++; streamCounts(); return; }
    if (!streamPasses(c)) return;
    if (streamQueue.length >= STREAM_MAXQ) { streamQueue.shift(); streamDropped++; }  // drop oldest, stay live
    streamQueue.push(c);
    streamCounts();
    renderStreamNext();
    if (!streamNowCall) streamPlayNext();
  };
  $("#stream-toggle")?.addEventListener("click", async () => {
    streaming = !streaming;
    $("#stream-dot").className = "tuner-dot " + (streaming ? "on" : "off");
    const st = $("#stream-state");
    st.textContent = streaming ? "LIVE" : "STOPPED";
    st.style.color = streaming ? "var(--neon-lime)" : "var(--dim)";
    $("#stream-toggle").textContent = streaming ? "STOP" : "START";
    if (streaming) {
      await loadTgAliasMap();
      renderStreamNow();
      if (streamQueue.length && !streamNowCall) streamPlayNext();
    } else {
      streamQueue = []; streamNowCall = null;
      streamAudio.pause(); streamAudio.removeAttribute("src"); streamAudio.load();
      renderStreamNow(); renderStreamNext();
    }
  });
  streamAudio?.addEventListener("ended", streamPlayNext);
  streamAudio?.addEventListener("error", () => { if (streaming) streamPlayNext(); });

  socket.on("call", c => {
    streamEnqueue(c);
    noteRecentCall(c);
    // merged transcript pane: pending row the transcript event will fill in
    if (tabActive("tx") && txFeedMatch(c) && !c.encrypted && c.audio
        && !txFeed?.querySelector(`.tx-item[data-call="${c.id}"]`)) {
      txFeedPrepend(txFeedRow(c));
    }
    bumpTile("s-calls", 1);
    if (c.encrypted) bumpTile("s-enc", 1);
    spawnPacket(c);
    lastWhoKey = `${c.source}|${c.tgid}`;
    whoLive();
    if (tabActive("who")) throttled("pres", 15000, loadPresences);
    if (monitorTg && String(c.tgid) === monitorTg) {
      monitorCallId = c.id;
      monTx.textContent = "";
      const who  = (c.rid_alias ? c.rid_alias + " " : "") + "RID " + (c.source || "?");
      const freq = c.frequency ? " · " + (c.frequency/1e6).toFixed(4) + " MHz" : "";
      if (c.frequency) monFreq.textContent = fmtFreq(c.frequency);
      if (c.encrypted || !c.audio) {
        monNote.textContent = `${fmtTs(c.ts)} · ${who} -- encrypted, skipped`;
      } else {
        monNote.textContent = `${fmtTs(c.ts)} · ${who}${freq} · ${c.system||""}`;
        monAudio.src = "/api/audio/" + c.audio;
        monAudio.play().catch(() => { monTx.textContent = "click the player once to allow sound"; });
      }
    }
  });
  socket.on("event", e => {
    prepend(liveEvents, evtRow(e));
    bumpTile("s-events", 1);
    spawnEventPacket(e);
    whoLive();
    // keep data tabs fresh while they're being watched
    if (tabActive("network"))   throttled("net",  10000, () => { loadSites(); loadDenies(); loadAffs(); });
    if (tabActive("directory")) throttled("dir",  20000, loadDirectory);
    if (tabActive("stats"))     throttled("stat", 30000, loadStats);
    if (tabActive("who")) throttled("pres", 15000, loadPresences);
  });
  socket.on("anomaly", a => {
    // If on ALERTS tab, refresh; otherwise just bump a small counter
    const alertsPanel = document.getElementById("tab-alerts");
    if (alertsPanel && alertsPanel.classList.contains("active")) loadAlerts();
  });
  socket.on("transcript", t => {
    const row = txFeed?.querySelector(`.tx-item[data-call="${t.call_id}"] .tx`);
    if (row) {
      row.classList.remove("pending");
      row.textContent = t.text || "(empty)";
    } else {
      // transcript for a call the pane doesn't show yet (arrived while we
      // were on another tab/filter): insert it if it matches the filter
      const c = recentCalls.get(t.call_id);
      if (c && tabActive("tx") && txFeedMatch(c))
        txFeedPrepend(txFeedRow({...c, transcript: t.text}));
    }
    if (monitorCallId != null && t.call_id === monitorCallId) {
      monTx.textContent = t.text || "";
    }
    whoLiveTx();
    bumpTile("s-tx", 1);
  });

  const bumpTile = (id, by) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = (parseInt(el.textContent||"0",10) + by).toLocaleString();
  };

  const tabActive = t => document.getElementById("tab-"+t)?.classList.contains("active");
  const _thr = {};
  const throttled = (key, ms, fn) => {
    const now = Date.now();
    if (now - (_thr[key]||0) > ms) { _thr[key] = now; fn(); }
  };

  // ---- initial load ----
  const refreshSummary = async () => {
    const r = await fetch("/api/stats/summary").then(r=>r.json());
    $("#s-calls").textContent  = r.calls.toLocaleString();
    $("#s-events").textContent = r.events.toLocaleString();
    $("#s-tgs").textContent    = r.talkgroups.toLocaleString();
    $("#s-rids").textContent   = r.radios.toLocaleString();
    $("#s-enc").textContent    = r.encrypted_calls.toLocaleString();
    $("#s-tx").textContent     = r.transcribed.toLocaleString();
    $("#s-tx-p").textContent   = r.pending_transcripts.toLocaleString();
  };

  const loadLive = async () => {
    const events = await fetch("/api/events?limit=100").then(r=>r.json());
    liveEvents.innerHTML = events.map(evtRow).join("");
  };

  // TGs & RADIOS tab (consolidated, grouped per network)
  const loadDirectory = async () => {
    const [tgs, rids] = await Promise.all([
      fetch("/api/talkgroups?limit=800").then(r=>r.json()),
      fetch("/api/radios?limit=800").then(r=>r.json()),
    ]);
    const bySystem = new Map();
    const bucket = sys => {
      if (!bySystem.has(sys)) bySystem.set(sys, {tgs: [], rids: []});
      return bySystem.get(sys);
    };
    for (const t of tgs)  bucket(t.system || "unknown").tgs.push(t);
    for (const r of rids) bucket(r.system || "unknown").rids.push(r);
    const systems = [...bySystem.entries()]
      .sort((a,b) => (b[1].tgs.length + b[1].rids.length) - (a[1].tgs.length + a[1].rids.length));
    $("#directory").innerHTML = systems.map(([sys, d]) => `
      <h3 class="tx-sys">${rrLink(sys, sys)} <span class="tx-sys-n">${d.tgs.length} TGs · ${d.rids.length} RIDs</span></h3>
      <div class="two-col">
        <div class="tx-box">
          <div class="tx-box-head"><span class="alias">TALKGROUPS</span><span class="n">${d.tgs.length}</span></div>
          <div class="tx-box-body dir-body">
            <table class="data">
              <thead><tr><th>TGID</th><th>ALIAS</th><th>GROUP</th><th>CALLS</th><th>AIR (s)</th><th>ENC</th><th>LAST</th></tr></thead>
              <tbody>${d.tgs.map(r => `<tr>
                <td class="click-tg" data-tg="${r.tgid}">${r.tgid}</td>
                <td>${aliasCell("tg", sys, r.tgid, rrLink(sys, r.alias||""))}</td>
                <td>${esc(r.tg_group||"")}</td>
                <td>${r.call_count}</td>
                <td>${(r.total_ms/1000).toFixed(1)}</td>
                <td class="${r.encrypted?"enc":""}">${r.encrypted?"YES":""}</td>
                <td>${r.last_seen? fmtTs(r.last_seen):""}</td>
              </tr>`).join("")}</tbody>
            </table>
          </div>
        </div>
        <div class="tx-box">
          <div class="tx-box-head"><span class="alias">RADIOS</span><span class="n">${d.rids.length}</span></div>
          <div class="tx-box-body dir-body">
            <table class="data">
              <thead><tr><th>RID</th><th>ALIAS</th><th>CALLS</th><th>AIR (s)</th><th>FIRST</th><th>LAST</th></tr></thead>
              <tbody>${d.rids.map(r => `<tr>
                <td class="click-rid" data-rid="${r.rid}">${r.rid}</td>
                <td>${aliasCell("rid", sys, r.rid, esc(r.alias||""))}</td>
                <td>${r.call_count}</td>
                <td>${(r.total_ms/1000).toFixed(1)}</td>
                <td>${r.first_seen? fmtTs(r.first_seen):""}</td>
                <td>${r.last_seen?  fmtTs(r.last_seen):""}</td>
              </tr>`).join("")}</tbody>
            </table>
          </div>
        </div>
      </div>`).join("");
    $$("#directory table.data").forEach(makeSortable);   // tables are re-rendered each load
  };

  // Topology tab
  let graphNet = null, graphNodes = null, graphEdges = null, topoById = {};
  // Live tx indicators: a glowing dot flows transmitter(RID) -> recipient(TG)
  // on every completed call. Cyan = clear, purple = encrypted.
  const packets = [];   // {a, b, t0, dur, enc}
  let packetAnim = null;
  const topoTick = () => {
    const now = performance.now();
    for (let i = packets.length-1; i >= 0; i--)
      if (now - packets[i].t0 > packets[i].dur) packets.splice(i, 1);
    const liveN = document.getElementById("topo-live-n");
    if (liveN) liveN.textContent = packets.length;
    if (packets.length && graphNet) {
      graphNet.redraw();
      packetAnim = requestAnimationFrame(topoTick);
    } else packetAnim = null;
  };
  const pushPacket = (a, b, enc, durMs) => {
    const now = performance.now();
    // dedupe: grant event + call completion for the same pair arrive ~seconds apart
    if (packets.some(p => p.a === a && p.b === b && now - p.t0 < 3000)) return;
    packets.push({ a, b, t0: now,
                   dur: Math.min(4000, Math.max(900, durMs || 1400)), enc: !!enc });
    if (!packetAnim) packetAnim = requestAnimationFrame(topoTick);
  };
  // Traffic seen after the graph was built: add the nodes/edges live so the
  // map shows connections forming in realtime, not just the 24h snapshot.
  const addTopoNode = (id, kind, label, dbid) => {
    graphNodes.add({ id, label, value: 1, title: label,
                     _kind: kind, _dbid: dbid, ...TOPO_STYLE[kind] });
    topoById[id] = graphNodes.get(id);
  };
  const topoEdgeExists = (a, b) =>
    graphEdges.get({ filter: e => e.from === a && e.to === b }).length > 0;
  const ensureTopoNodes = (sid, fromRid, toTg, toRid) => {
    let a = null, b = null, added = false;
    if (fromRid != null) {
      a = `rid:${sid}:${fromRid}`;
      if (!graphNodes.get(a)) { addTopoNode(a, "rid", `RID ${fromRid}`, fromRid); added = true; }
    }
    if (toTg != null) {
      b = `tg:${sid}:${toTg}`;
      if (!graphNodes.get(b)) {
        addTopoNode(b, "tg", `TG ${toTg}`, toTg);
        added = true;
        if (graphNodes.get(`sys:${sid}`))
          graphEdges.add({ from: `sys:${sid}`, to: b, value: 1,
                           color: { color: "#4a6fa5", opacity: 0.35 } });
      }
    } else if (toRid != null) {
      b = `rid:${sid}:${toRid}`;
      if (!graphNodes.get(b)) { addTopoNode(b, "rid", `RID ${toRid}`, toRid); added = true; }
    }
    if (a && b && !topoEdgeExists(a, b)) {
      graphEdges.add({ from: a, to: b, value: 1,
                       color: { color: "#29ffe6", opacity: 0.35 } });
      added = true;
    }
    if (added) topoApplyView();   // keep focus filters applied to new nodes
    return { a, b };
  };
  const spawnPacket = c => {           // call completion (carries system_id)
    if (!graphNet || !graphNodes || c.system_id == null) return;
    if (c.tgid == null || c.source == null) return;
    const { a, b } = ensureTopoNodes(c.system_id, c.source, c.tgid, null);
    if (a && b) pushPacket(a, b, c.encrypted);
  };
  const sysIdByName = {};
  const spawnEventPacket = e => {      // control-channel grant: realtime key-up
    if (!graphNet || !graphNodes) return;
    if (!/call/i.test(e.type || "")) return;
    const sid = sysIdByName[canonSys(e.system)];
    if (sid == null) return;
    if (e.to_tg == null && e.to_rid == null) return;
    const { a, b } = ensureTopoNodes(sid, e.from, e.to_tg, e.to_rid);
    if (!b) return;
    if (a) { pushPacket(a, b, e.encrypted, e.duration_ms); return; }
    // grant with no FROM (common at key-up): pulse the target node instead
    pushPacket(null, b, e.encrypted, e.duration_ms);
  };
  const drawPackets = ctx => {
    if (!packets.length || !graphNet) return;
    const now = performance.now();
    for (const p of packets) {
      const t = Math.min(1, (now - p.t0) / p.dur);
      const col = p.enc ? "#a06bff" : "#29ffe6";
      if (p.a == null) {
        // sender unknown (grant had no FROM): expanding ring on the target node
        const pos = graphNet.getPositions([p.b])[p.b];
        if (!pos) continue;
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, 6 + t * 26, 0, 2 * Math.PI);
        ctx.strokeStyle = col;
        ctx.globalAlpha = 0.9 * (1 - t);
        ctx.lineWidth = 2.5;
        ctx.shadowColor = col;
        ctx.shadowBlur = 10;
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.shadowBlur = 0;
        continue;
      }
      const pos = graphNet.getPositions([p.a, p.b]);
      if (!pos[p.a] || !pos[p.b]) continue;
      // light up the whole edge while the transmission flows
      ctx.beginPath();
      ctx.moveTo(pos[p.a].x, pos[p.a].y);
      ctx.lineTo(pos[p.b].x, pos[p.b].y);
      ctx.strokeStyle = col;
      ctx.globalAlpha = 0.85 * (1 - t * 0.5);
      ctx.lineWidth = 3.5;
      ctx.shadowColor = col;
      ctx.shadowBlur = 8;
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.shadowBlur = 0;
      for (let k = 3; k >= 0; k--) {          // head + fading trail
        const tk = Math.max(0, t - k * 0.05);
        const x = pos[p.a].x + (pos[p.b].x - pos[p.a].x) * tk;
        const y = pos[p.a].y + (pos[p.b].y - pos[p.a].y) * tk;
        ctx.beginPath();
        ctx.arc(x, y, k === 0 ? 5 : 2.5, 0, 2 * Math.PI);
        ctx.fillStyle = col;
        ctx.globalAlpha = k === 0 ? 1 : 0.35 - k * 0.08;
        ctx.shadowColor = col;
        ctx.shadowBlur = k === 0 ? 16 : 0;
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      ctx.shadowBlur = 0;
    }
  };
  const TOPO_STYLE = {
    system: { color: "#ffb800", shape: "diamond",  size: 16 },
    site:   { color: "#b4ff2b", shape: "triangle", size: 11 },
    tg:     { color: "#29ffe6", shape: "square",   size: 10 },
    rid:    { color: "#ff2bd6", shape: "dot",      size: 7  },
  };
  // focus-panel state: which kinds are shown, and top-N activity highlight
  const topoState = { kinds: { system: true, site: true, tg: true, rid: true }, topN: 0 };
  const topoApplyView = () => {
    if (!graphNodes || !graphEdges) return;
    const all = graphNodes.get();
    const ranked = [...all].sort((x, y) => (y.value || 0) - (x.value || 0));
    const topIds = new Set(topoState.topN
      ? ranked.slice(0, topoState.topN).map(n => n.id) : []);
    graphNodes.update(all.map(n => {
      const hide = topoState.kinds[n._kind] === false;
      const dim  = topoState.topN && !hide && !topIds.has(n.id);
      return {
        id: n.id,
        hidden: hide,
        color: dim ? "rgba(74,111,165,0.15)" : TOPO_STYLE[n._kind].color,
        font: { color: dim ? "rgba(109,132,168,0.35)" : "#d8e6ff",
                face: "monospace", size: 11 },
      };
    }));
  };
  $$("#topo-panel input[data-kind]").forEach(cb =>
    cb.addEventListener("change", () => {
      topoState.kinds[cb.dataset.kind] = cb.checked;
      topoApplyView();
    }));
  $("#topo-topn")?.addEventListener("change", e => {
    topoState.topN = parseInt(e.target.value, 10) || 0;
    topoApplyView();
  });
  $("#topo-q")?.addEventListener("input", e => topoSearch(e.target.value));
  $("#topo-refit")?.addEventListener("click", () =>
    graphNet?.fit({ animation: { duration: 400 } }));
  const loadGraph = async () => {
    const d = await fetch("/api/graph/topology?hours=24").then(r=>r.json());
    for (const n of d.nodes) if (n.kind === "system") sysIdByName[n.label] = n.dbid;
    topoById = {};
    const nodes = d.nodes.map(n => {
      const v = { id: n.id, label: n.label, value: Math.max(1, n.n),
                  title: `${n.title || n.label} — ${n.n} events`,
                  _kind: n.kind, _dbid: n.dbid, ...TOPO_STYLE[n.kind] };
      topoById[n.id] = v;
      return v;
    });
    const edges = d.edges.map(e => ({
      from: e.from, to: e.to, value: Math.max(1, e.w),
      color: { color: e.kind === "tx" ? "#29ffe6" : "#4a6fa5", opacity: 0.35 },
    }));
    const container = $("#topology-graph");
    if (graphNet) graphNet.destroy();
    graphNodes = new vis.DataSet(nodes);
    graphEdges = new vis.DataSet(edges);
    graphNet = new vis.Network(container, {
      nodes: graphNodes, edges: graphEdges
    }, {
      nodes: { font: { color: "#d8e6ff", face: "monospace", size: 11 },
               scaling: { min: 6, max: 30 } },
      edges: { smooth: false, scaling: { min: 1, max: 6 } },
      physics: { stabilization: true,
                 barnesHut: { gravitationalConstant: -4000, springLength: 120 } },
      layout: { improvedLayout: false },
      interaction: { hover: true, tooltipDelay: 120 },
    });
    graphNet.on("afterDrawing", drawPackets);
    topoApplyView();   // default top-10 highlight on load
    graphNet.on("click", params => {
      const nid = params.nodes[0];
      if (!nid) return;
      const n = topoById[nid];
      if (!n) return;
      if (n._kind === "rid")         showRid(n._dbid);
      else if (n._kind === "tg")     showTg(n._dbid);
      else if (n._kind === "site")   showSite(n._dbid);
      else if (n._kind === "system") showSystem(n._dbid);
    });
  };

  // Stats tab
  let hourChart = null;
  const loadStats = async () => {
    const hours = await fetch("/api/stats/by_hour?hours=24").then(r=>r.json());
    const labels = hours.map(h => new Date(h.hour*3600*1000).toISOString().slice(11,16));
    const totals = hours.map(h => h.n);
    const encs   = hours.map(h => h.enc || 0);
    if (hourChart) hourChart.destroy();
    hourChart = new Chart($("#chart-hour"), {
      type: "bar",
      data: { labels, datasets: [
        { label: "calls", data: totals, backgroundColor: "#29ffe6" },
        { label: "encrypted", data: encs, backgroundColor: "#a06bff" }
      ]},
      options: { responsive: true, scales: { x: { stacked: true, ticks:{color:"#6d84a8"}},
                                             y: { stacked: true, ticks:{color:"#6d84a8"}}},
                 plugins: { legend: { labels: { color: "#d8e6ff" } } } }
    });
    const hm = await fetch("/api/stats/tg_heatmap?hours=24&top=30").then(r=>r.json());
    renderHeatmap(hm);
  };

  const renderHeatmap = ({tgs, cells}) => {
    if (!tgs.length) { $("#heatmap").innerHTML = "<em>no data</em>"; return; }
    const nowH = Math.floor(Date.now()/3600000);
    const hours = Array.from({length:24}, (_,i) => nowH - 23 + i);
    const map = new Map();
    for (const c of cells) map.set(`${c.tgid}:${c.hour}`, c.n);
    const max = Math.max(1, ...cells.map(c=>c.n));
    let html = "<table><thead><tr><th>TG</th>" +
      hours.map(h => `<th>${(h%24).toString().padStart(2,"0")}</th>`).join("") + "</tr></thead><tbody>";
    for (const tg of tgs) {
      html += `<tr><th title="${esc(tg.alias||'')}">${tg.alias?esc(tg.alias):tg.tgid}</th>`;
      for (const h of hours) {
        const n = map.get(`${tg.tgid}:${h}`) || 0;
        const a = n / max;
        const bg = `rgba(41,255,230,${0.05 + 0.8*a})`;
        html += `<td style="background:${bg}" title="${n} calls">${n||""}</td>`;
      }
      html += "</tr>";
    }
    html += "</tbody></table>";
    $("#heatmap").innerHTML = html;
  };

  // Search tab
  let searchTimer = null;
  $("#search-q")?.addEventListener("input", e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = e.target.value.trim();
      if (!q) { $("#search-results").innerHTML = ""; $("#search-count").textContent = ""; return; }
      const rows = await fetch("/api/search?q=" + encodeURIComponent(q)).then(r=>r.json());
      $("#search-count").textContent = rows.length
        ? `${rows.length} match${rows.length===1?"":"es"} — click a RID or TG to drill down`
        : "no matches";
      $("#search-results").innerHTML = rows.map(r => `<div class="row ${r.encrypted?"enc":""}">
        <span class="ts">${fmtTs(r.ts)}</span>
        <span class="sys">${rrLink(r.system, r.system||"")}</span>
        <span class="rid click-rid" data-rid="${r.source_rid||""}">RID ${esc(r.source_rid||"")}</span>
        <span class="tg"><span class="alias">${rrLink(r.system, r.tg_alias||"")}</span><span class="id click-tg" data-tg="${r.tgid||""}">${r.tgid||""}</span></span>
        <span class="meta">${r.encrypted?'<span class="enc">ENC</span>':""}</span>
        ${r.audio_path ? `<audio controls preload="none" src="/api/audio/${esc(r.audio_path)}"></audio>`:""}
        <div class="tx">${esc(r.transcript||"")}</div>
      </div>`).join("");
    }, 200);
  });

  // ---- generic table sorting: click a th to sort, again to reverse ----
  // Idempotent; loaders that replace only tbody keep the th listeners.
  const makeSortable = table => {
    if (!table || table.dataset.sortable) return;
    table.dataset.sortable = "1";
    const num = s => {
      const t = s.replace(/[^\d.\-]/g, "");
      return t !== "" && !isNaN(t) ? parseFloat(t) : null;
    };
    table.querySelectorAll("thead th").forEach((th, col) => {
      th.classList.add("sortable");
      th.addEventListener("click", () => {
        const tbody = table.tBodies[0];
        if (!tbody) return;
        const asc = th.dataset.dir !== "asc";
        table.querySelectorAll("thead th").forEach(h => delete h.dataset.dir);
        th.dataset.dir = asc ? "asc" : "desc";
        [...tbody.rows].sort((a, b) => {
          const av = a.cells[col]?.textContent.trim() ?? "";
          const bv = b.cells[col]?.textContent.trim() ?? "";
          const an = num(av), bn = num(bv);
          return (an !== null && bn !== null ? an - bn : av.localeCompare(bv)) * (asc ? 1 : -1);
        }).forEach(r => tbody.appendChild(r));
      });
    });
  };

  // ---- per-tab filter bars ----
  const applyFilter = (panel, q) => {
    q = q.trim().toLowerCase();
    panel.querySelectorAll(".tx-item, .feed .row, tbody tr").forEach(r => {
      r.style.display = !q || r.textContent.toLowerCase().includes(q) ? "" : "none";
    });
    panel.querySelectorAll(".tx-box").forEach(box => {
      if (!q) { box.style.display = ""; return; }
      const head = box.querySelector(".tx-box-head");
      if (head && head.textContent.toLowerCase().includes(q)) {
        box.style.display = "";
        box.querySelectorAll(".tx-item, tbody tr").forEach(r => r.style.display = "");
        return;
      }
      const anyVis = Array.from(box.querySelectorAll(".tx-item, tbody tr"))
        .some(r => r.style.display !== "none");
      box.style.display = anyVis ? "" : "none";
    });
    panel.querySelectorAll("h3.tx-sys").forEach(h => {
      if (!q) { h.style.display = ""; return; }
      const selfHit = h.textContent.toLowerCase().includes(q);
      let el = h.nextElementSibling, vis = selfHit;
      while (el && !el.matches("h3.tx-sys")) {
        if (selfHit) {   // system name matched: keep its boxes visible
          if (el.matches(".tx-box")) el.style.display = "";
          el.querySelectorAll?.(".tx-box").forEach(b => b.style.display = "");
        } else {
          const boxes = el.matches(".tx-box") ? [el]
                      : Array.from(el.querySelectorAll(".tx-box"));
          const cand = boxes.length ? boxes : [el];
          if (cand.some(b => b.style.display !== "none")) vis = true;
        }
        el = el.nextElementSibling;
      }
      h.style.display = vis ? "" : "none";
    });
  };

  const topoSearch = q => {
    if (!graphNet) return;
    q = q.trim().toLowerCase();
    if (!q) { graphNet.unselectAll(); return; }
    const ids = Object.keys(topoById)
      .filter(id => (topoById[id].label || "").toLowerCase().includes(q));
    if (ids.length) {
      graphNet.selectNodes(ids);
      graphNet.fit({ nodes: ids, animation: { duration: 400 } });
    } else graphNet.unselectAll();
  };

  $$(".tab-filter").forEach(inp => inp.addEventListener("input", () => {
    const panel = inp.closest(".tab-panel");
    if (!panel) return;
    if (panel.id === "tab-topology") topoSearch(inp.value);
    else applyFilter(panel, inp.value);
  }));
  // re-apply the active tab's filter as live content streams in under it
  setInterval(() => {
    const panel = document.querySelector(".tab-panel.active");
    const inp = panel?.querySelector(".tab-filter");
    if (inp && inp.value.trim() && panel.id !== "tab-topology")
      applyFilter(panel, inp.value);
  }, 3000);

  // ---- SITES tab ----
  const loadSites = async () => {
    const rows = await fetch("/api/sites").then(r=>r.json());
    $("#site-table tbody").innerHTML = rows.map(r => {
      const encPct = r.events ? ((r.enc_events/r.events)*100).toFixed(1) : "0.0";
      return `<tr>
        <td>${esc(r.system||"")}</td>
        <td>${esc(r.site_name||r.site_id||"")}</td>
        <td>${r.events}</td>
        <td>${r.calls}</td>
        <td class="${r.enc_events?"enc":""}">${r.enc_events}</td>
        <td class="${r.enc_events?"enc":""}">${encPct}%</td>
        <td>${r.unique_rids}</td>
        <td>${r.first_seen?fmtTs(r.first_seen):""}</td>
        <td>${r.last_seen?fmtTs(r.last_seen):""}</td>
      </tr>`;
    }).join("");
  };

  // ---- ENC tab ----
  let encChart = null;
  const loadEnc = async () => {
    const d = await fetch("/api/stats/encryption").then(r=>r.json());
    const labels = d.per_system.map(x=>x.system);
    const enc = d.per_system.map(x=>x.enc||0);
    const clear = d.per_system.map(x=>(x.total||0)-(x.enc||0));
    if (encChart) encChart.destroy();
    encChart = new Chart($("#chart-enc-system"), {
      type: "bar",
      data: { labels, datasets: [
        { label: "clear",     data: clear, backgroundColor: "#29ffe6", stack: "s" },
        { label: "encrypted", data: enc,   backgroundColor: "#a06bff", stack: "s" }
      ]},
      options: { responsive: true, scales: { x: { stacked: true, ticks:{color:"#6d84a8"}},
                                             y: { stacked: true, ticks:{color:"#6d84a8"}}},
                 plugins: { legend: { labels: { color: "#d8e6ff" } } } }
    });
    $("#enc-tg-table tbody").innerHTML = d.encrypted_talkgroups.map(r => `<tr>
      <td>${esc(r.system||"")}</td>
      <td class="click-tg" data-tg="${r.tgid}">${r.tgid}</td>
      <td>${esc(r.alias||"")}</td>
      <td>${esc(r.tg_group||"")}</td>
      <td>${r.call_count}</td>
    </tr>`).join("");
  };

  // ---- DENIES tab ----
  const loadDenies = async () => {
    const rows = await fetch("/api/denies").then(r=>r.json());
    $("#deny-table tbody").innerHTML = rows.map(r => `<tr>
      <td>${fmtTs(r.ts)}</td>
      <td>${esc(r.system||"")}</td>
      <td>${esc(r.site||"")}</td>
      <td class="${r.rid?"click-rid":""}" data-rid="${r.rid||""}">${r.rid||""}</td>
      <td class="${r.tgid?"click-tg":""}" data-tg="${r.tgid||""}">${esc(r.tg_alias||"")} ${r.tgid?"("+r.tgid+")":""}</td>
      <td>${esc(r.reason||"")}</td>
    </tr>`).join("");
  };

  // ---- AFFS tab ----
  const loadAffs = async () => {
    const rows = await fetch("/api/affiliations").then(r=>r.json());
    $("#aff-table tbody").innerHTML = rows.map(r => `<tr>
      <td>${esc(r.system||"")}</td>
      <td class="click-rid" data-rid="${r.rid}">${r.rid}</td>
      <td class="click-tg"  data-tg="${r.tgid}">${r.tgid}</td>
      <td>${esc(r.tg_alias||"")}</td>
      <td>${r.n}</td>
      <td>${r.last_ts?fmtTs(r.last_ts):""}</td>
    </tr>`).join("");
  };

  // ---- WHO'S TALKING tab ----
  let lastWhoKey = null, lastWhoLoad = 0, lastWhoTxLoad = 0;
  const loadWho = async () => {
    lastWhoLoad = Date.now();
    const mins = $("#who-minutes")?.value || "15";
    const rows = await fetch(`/api/whos_talking?minutes=${mins}`).then(r=>r.json());
    $("#who-table tbody").innerHTML = rows.map(r => `<tr data-key="${r.rid}|${r.tgid}">
      <td>${rrLink(r.system, r.system||"")}</td>
      <td class="click-rid" data-rid="${r.rid}">${r.rid}${newBadge(r.rid_first_seen)}</td>
      <td>${aliasCell("rid", r.system, r.rid, esc(r.rid_alias||""))}</td>
      <td class="click-tg" data-tg="${r.tgid}">${r.tgid}${newBadge(r.tg_first_seen)}</td>
      <td>${aliasCell("tg", r.system, r.tgid, rrLink(r.system, r.tg_alias||""))}</td>
      <td class="who-freq">${fmtFreq(r.last_freq)}</td>
      <td>${esc(r.tg_group||"")}</td>
      <td>${r.n}</td>
      <td>${((r.total_ms||0)/1000).toFixed(1)}</td>
      <td class="${r.enc?"enc":""}">${r.enc||""}</td>
      <td class="who-tx">${esc(r.last_tx||"")}</td>
      <td>${fmtTs(r.last_ts)}</td>
    </tr>`).join("") || `<tr><td colspan="12" style="color:var(--dim);text-align:center;padding:20px">no traffic in the last ${mins} minutes</td></tr>`;
    if (lastWhoKey) {
      const tr = $(`#who-table tr[data-key="${CSS.escape(lastWhoKey)}"]`);
      if (tr) {
        tr.classList.add("fresh");
        setTimeout(() => tr.classList.remove("fresh"), 1400);
      }
    }
  };
  const whoTabActive = () => document.getElementById("tab-who")?.classList.contains("active");
  const whoLive = () => {           // throttled live refresh on new calls
    if (!whoTabActive()) return;
    if (Date.now() - lastWhoLoad > 2000) loadWho();
  };
  const whoLiveTx = () => {         // slower throttle for transcript fill-in
    if (!whoTabActive()) return;
    const now = Date.now();
    if (now - lastWhoTxLoad > 8000) { lastWhoTxLoad = now; loadWho(); }
  };
  $("#who-minutes")?.addEventListener("change", loadWho);

  // ---- PRESENCES tab (RIDs/TGs first seen in the last N hours) ----
  const loadPresences = async () => {
    const hours = $("#presences-hours")?.value || "24";
    const d = await fetch(`/api/presences?hours=${hours}`).then(r=>r.json());
    const now = Date.now()/1000;
    // still-on-the-air marker, same dot convention as the TUNERS tab
    const activeDot = ts => (ts && now - ts < 900)
      ? `<span class="tuner-dot on" title="active in the last 15m"></span>` : "";
    $("#presences-rid-n").textContent = d.radios.length;
    $("#presences-tg-n").textContent  = d.talkgroups.length;
    $("#presences-rid-table tbody").innerHTML = d.radios.map(r => `<tr>
      <td>${activeDot(r.last_seen)}</td>
      <td class="click-rid" data-rid="${r.rid}">${r.rid}</td>
      <td>${aliasCell("rid", r.system, r.rid, esc(r.alias||""))}</td>
      <td>${esc(r.system||"")}</td>
      <td>${r.call_count}</td>
      <td>${((r.total_ms||0)/1000).toFixed(1)}</td>
      <td title="${fmtTs(r.first_seen)}">${fmtAgo(r.first_seen)}</td>
    </tr>`).join("") || `<tr><td colspan="7" style="color:var(--dim);text-align:center;padding:20px">no new radios in this window</td></tr>`;
    $("#presences-tg-table tbody").innerHTML = d.talkgroups.map(t => `<tr>
      <td>${activeDot(t.last_seen)}</td>
      <td class="click-tg" data-tg="${t.tgid}">${t.tgid}</td>
      <td>${aliasCell("tg", t.system, t.tgid, rrLink(t.system, t.alias||""))}</td>
      <td>${esc(t.tg_group||"")}</td>
      <td>${esc(t.system||"")}</td>
      <td>${t.call_count}</td>
      <td class="${t.encrypted?"enc":""}">${t.encrypted?"YES":""}</td>
      <td title="${fmtTs(t.first_seen)}">${fmtAgo(t.first_seen)}</td>
    </tr>`).join("") || `<tr><td colspan="8" style="color:var(--dim);text-align:center;padding:20px">no new talkgroups in this window</td></tr>`;
  };
  $("#presences-hours")?.addEventListener("change", loadPresences);

  // ---- ALERTS tab ----
  let anomKindChart = null, newRidChart = null;
  const KIND_COLORS = {
    new_rid: "#29ffe6", new_tg: "#a06bff", new_site: "#ffb800",
    new_rid_on_tg: "#ff2bd6", new_rid_at_site: "#b4ff2b", spike: "#ff3050",
  };
  const loadAlerts = async () => {
    const kind   = $("#alerts-kind")?.value || "";
    const system = $("#alerts-system")?.value || "";
    const hours  = $("#alerts-hours")?.value || "";
    let url = "/api/anomalies?limit=300";
    if (kind)   url += "&kind=" + kind;
    if (system) url += "&system=" + encodeURIComponent(system);
    if (hours)  url += "&hours=" + hours;
    const rows = await fetch(url).then(r=>r.json());
    $("#alerts-table tbody").innerHTML = rows.map(a => `<tr>
      <td>${fmtTs(a.ts)}</td>
      <td style="color:${KIND_COLORS[a.kind]||'#fff'}">${esc(a.kind)}</td>
      <td>${esc(a.system||"")}</td>
      <td class="${a.rid?"click-rid":""}" data-rid="${a.rid||""}">${a.rid||""}</td>
      <td class="${a.tgid?"click-tg":""}" data-tg="${a.tgid||""}">${a.tgid||""}</td>
      <td style="color:var(--dim)">${esc(a.details||"")}</td>
    </tr>`).join("") || `<tr><td colspan="6" style="color:var(--dim);text-align:center;padding:20px">no anomalies in this window</td></tr>`;

    let surl = "/api/anomalies/stats?x=1";
    if (system) surl += "&system=" + encodeURIComponent(system);
    if (hours)  surl += "&hours=" + hours;
    const stats = await fetch(surl).then(r=>r.json());
    // kind dropdown + system dropdown: populate once from what's been seen
    const kindSel = $("#alerts-kind");
    if (kindSel && kindSel.options.length <= 1) {
      const prev = kind;
      kindSel.innerHTML = `<option value="">all kinds</option>` +
        (stats.kinds||[]).map(k => `<option value="${k}">${k.replace(/_/g," ")}</option>`).join("");
      kindSel.value = prev;
    }
    const sysSel = $("#alerts-system");
    if (sysSel && sysSel.options.length <= 1) {
      const systems = await fetch("/api/systems").then(r=>r.json());
      sysSel.innerHTML = `<option value="">all systems</option>` +
        systems.map(s => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
      sysSel.value = system;
    }
    // per-kind count chips for the current window/system
    $("#alerts-chips").innerHTML = stats.per_kind.map(k =>
      `<span class="chip" style="color:${KIND_COLORS[k.kind]||'#4a6fa5'}">${esc(k.kind)} ×${k.n}</span>`).join("");
    if (anomKindChart) anomKindChart.destroy();
    anomKindChart = new Chart($("#chart-anom-kind"), {
      type: "doughnut",
      data: {
        labels: stats.per_kind.map(k=>k.kind),
        datasets: [{ data: stats.per_kind.map(k=>k.n),
          backgroundColor: stats.per_kind.map(k=>KIND_COLORS[k.kind]||"#4a6fa5") }]
      },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: "right", labels: { color:"#d8e6ff", font:{size:10}, boxWidth: 10 } } } }
    });
    const labels = stats.new_rid_by_hour.map(h => new Date(h.hour*3600*1000).toISOString().slice(11,16));
    if (newRidChart) newRidChart.destroy();
    newRidChart = new Chart($("#chart-newrid"), {
      type: "line",
      data: { labels, datasets: [{ label:"new RIDs", data: stats.new_rid_by_hour.map(h=>h.n),
        borderColor: "#29ffe6", backgroundColor: "rgba(41,255,230,0.2)", tension: 0.2, fill: true }]},
      options: { scales: { x:{ticks:{color:"#6d84a8"}}, y:{ticks:{color:"#6d84a8"}}},
                 plugins: { legend: { labels: { color:"#d8e6ff" } } } }
    });
    syncAlertsHeight();
    // Chart.js settles its responsive size after render/animation
    setTimeout(syncAlertsHeight, 700);
  };
  $("#alerts-kind")?.addEventListener("change", loadAlerts);
  $("#alerts-system")?.addEventListener("change", loadAlerts);
  $("#alerts-hours")?.addEventListener("change", loadAlerts);

  // Keep the anomalies viewport bottom-aligned with the (sticky) graphs column
  const syncAlertsHeight = () => {
    const panel = document.getElementById("tab-alerts");
    if (!panel || !panel.classList.contains("active")) return;
    const body  = panel.querySelector(".col:first-child .tx-box-body");
    const right = panel.querySelector(".col:last-child");
    if (!body || !right) return;
    // mobile (single-column, nothing sticky): clear any synced height and bail
    if (window.innerWidth <= 820) { body.style.maxHeight = ""; return; }
    const delta = right.getBoundingClientRect().bottom -
                  body.getBoundingClientRect().bottom;
    if (Math.abs(delta) > 2) {
      body.style.maxHeight = Math.max(200, body.offsetHeight + delta) + "px";
    }
  };
  window.addEventListener("resize", syncAlertsHeight);

  // ---- TRANSCRIPTS tab: one live feed; dropdowns hone in on a convo ----
  const txFeed = $("#tx-feed");
  const recentCalls = new Map();   // call_id -> WS call, for transcript fill-in
  const noteRecentCall = c => {
    if (c.id == null) return;
    recentCalls.set(c.id, c);
    if (recentCalls.size > 500) recentCalls.delete(recentCalls.keys().next().value);
  };

  const txFeedMatch = c => {
    const sys = $("#tx-system")?.value || "";
    const tg  = $("#tx-convo")?.value || "";
    if (sys && canonSys(c.system) !== sys) return false;
    if (tg && String(c.tgid) !== tg) return false;
    return true;
  };

  // one row shape for API history rows and live WS calls
  const txFeedRow = r => {
    const rid   = r.source_rid ?? r.source;
    const audio = r.audio_path ?? r.audio;
    const alias = r.tg_alias || r.tg_label || "";
    const tx = r.encrypted ? `<span class="tx pending">[encrypted]</span>`
             : r.transcript ? `<span class="tx">${esc(r.transcript)}</span>`
             : `<span class="tx pending">transcribing...</span>`;
    return `<div class="tx-item ${r.encrypted?"enc":""}" data-call="${r.id}">
      <span class="ts">${fmtTs(r.ts)}</span>
      <span class="rid click-tg" data-tg="${r.tgid||""}">${esc(alias || "TG "+(r.tgid||""))}</span>
      <span class="rid click-rid" data-rid="${rid||""}">${esc(r.rid_alias||"") || "RID "+(rid ?? "?")}</span>
      <span class="freq">${fmtFreq(r.frequency)}</span>
      <button class="listen" data-listen-tg="${esc(r.tgid)}" data-listen-name="${esc(alias || "TG "+r.tgid)}" data-listen-freq="${r.frequency||""}">LISTEN</button>
      ${audio ? `<audio controls preload="none" src="/api/audio/${esc(audio)}"></audio>` : "<span></span>"}
      ${tx}
    </div>`;
  };

  const txFeedPrepend = html => {
    if (!txFeed) return;
    txFeed.insertAdjacentHTML("afterbegin", html);
    while (txFeed.childElementCount > 300) txFeed.lastElementChild.remove();
    if (!txFeed.dataset.frozen) txFeed.scrollTop = 0;   // newest on top
    const n = $("#tx-n");
    if (n) n.textContent = (parseInt(n.textContent||"0",10) + 1).toLocaleString();
  };

  // newest-on-top tail behavior: live rows pin the view to the top unless the
  // user scrolled down to read history; scrolling back up re-engages
  txFeed?.addEventListener("scroll", () => {
    if (txFeed.scrollTop < 24) delete txFeed.dataset.frozen;
    else txFeed.dataset.frozen = "1";
  });

  const loadTxFilters = async () => {
    const sysSel = $("#tx-system"), convo = $("#tx-convo");
    if (!sysSel || !convo) return;
    if (sysSel.options.length <= 1) {
      const systems = await fetch("/api/systems").then(r=>r.json());
      sysSel.innerHTML = `<option value="">ALL SYSTEMS</option>` +
        systems.map(s => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
    }
    const tgs = await fetch("/api/talkgroups?limit=800" +
      (sysSel.value ? `&system=${encodeURIComponent(sysSel.value)}` : "")).then(r=>r.json());
    const prev = convo.value;
    convo.innerHTML = `<option value="">ALL CONVERSATIONS</option>` +
      tgs.map(t => `<option value="${t.tgid}">${esc(t.alias || "TG "+t.tgid)} (${t.tgid})</option>`).join("");
    convo.value = prev;   // falls back to "" if the convo isn't in this system
  };

  const loadTX = async () => {
    const only = $("#tx-only-speech")?.checked ? "1" : "0";
    const sys  = $("#tx-system")?.value || "";
    const tg   = $("#tx-convo")?.value || "";
    let url = `/api/transcripts?limit=200&only_speech=${only}`;
    if (sys) url += `&system=${encodeURIComponent(sys)}`;
    if (tg)  url += `&tgid=${tg}`;
    const rows = await fetch(url).then(r=>r.json());
    txFeed.innerHTML = rows.map(txFeedRow).join("") ||
      `<div style="padding:14px;color:var(--dim)">no transcripts yet -- audio needs JMBE library set in SDRTrunk User Preferences</div>`;
    $("#tx-n").textContent = rows.length.toLocaleString();
    delete txFeed.dataset.frozen;
    txFeed.scrollTop = 0;
  };
  $("#tx-system")?.addEventListener("change", async () => { await loadTxFilters(); loadTX(); });
  $("#tx-convo")?.addEventListener("change", loadTX);
  const txToggle = $("#tx-only-speech");
  if (txToggle) txToggle.addEventListener("change", loadTX);

  // ---- TUNERS tab ----
  const fmtFreq = hz => hz ? (hz/1e6).toFixed(4)+" MHz" : "--";
  const loadTuners = async () => {
    const d = await fetch("/api/tuners").then(r=>r.json());
    if (d.error) {
      $("#tuner-cards").innerHTML = `<div style="color:var(--neon-amber);padding:12px">error: ${esc(d.error)}</div>`;
      return;
    }
    $("#tuner-cards").innerHTML = d.tuners.map(t => {
      const band = t.frequency > 700_000_000 && t.frequency < 800_000_000 ? "700 MHz block"
                 : t.frequency > 800_000_000 && t.frequency < 900_000_000 ? "800 MHz block"
                 : "other";
      const rateNum = (t.sampleRate||"").replace("RATE_","").replace("_",".");
      const rate = rateNum ? rateNum+" Msps" : "unknown";
      const dot = t.connected == null ? ""
        : `<span class="tuner-dot ${t.connected ? "on" : "off"}"></span>
           <span style="color:${t.connected ? "var(--neon-lime)" : "var(--neon-red)"};font-size:10px;letter-spacing:2px">
             ${t.connected ? "ACTIVE" : "SILENT"}</span>`;
      const title = (t.label || "SDR") + (t.uniqueID ? " — " + t.uniqueID : "");
      return `<div style="border:1px solid var(--border);background:var(--panel);padding:14px;margin-bottom:10px">
        <div style="color:var(--neon-cyan);letter-spacing:2px;font-size:14px">${dot} ${esc(title)}</div>
        <div style="color:var(--neon-mag);font-size:12px;margin-top:6px">center: ${fmtFreq(t.frequency)} &nbsp; ${band}</div>
        <div style="color:var(--dim);font-size:11px;margin-top:4px">sample rate: ${esc(rate)} &nbsp; LNA: ${esc(t.lnagain||"?")} &nbsp; VGA: ${esc(t.vgagain||"?")} &nbsp; amp: ${t.amplifierEnabled?"ON":"off"}</div>
      </div>`;
    }).join("") || `<div style="color:var(--dim);padding:12px">no tuners configured yet</div>`;
    const note = document.getElementById("tuner-boards");
    if (note) note.textContent = (d.boards_found ? `SDRs detected on USB: ${d.boards_found}. ` : "")
      + `a tuner is ACTIVE when traffic was seen in its receive window recently`;
    loadCoverage();
  };

  const loadCoverage = async () => {
    const d = await fetch("/api/coverage").then(r=>r.json());
    const win = d.windows.map(w =>
      `<div style="color:var(--dim);font-size:11px;margin-bottom:4px">
         <span style="color:var(--neon-cyan)">${esc(w.uid||"?")}</span>
         &nbsp;covers ${w.lo_mhz.toFixed(3)} – ${w.hi_mhz.toFixed(3)} MHz</div>`).join("");
    // only the targets we're actually receiving -- no gaps, no TBDs
    const rows = d.targets.filter(t => t.covered).map(t =>
      `<div style="display:flex;gap:10px;align-items:baseline;padding:5px 10px;border-bottom:1px solid #0f1729;font-size:12px">
        <span class="tuner-dot on"></span>
        <span style="color:var(--text)">${esc(t.name)}</span>
        <span style="color:var(--neon-mag);font-size:11px">${esc(t.kind||"")}</span>
        <span style="color:var(--dim);font-size:11px">${t.freqs.map(f=>f.toFixed(4)).join(", ")}</span>
      </div>`).join("");
    $("#coverage-panel").innerHTML =
      `<div class="tx-box"><div class="tx-box-body" style="max-height:none;padding:6px 0">${win}${rows}</div></div>`;
  };

  // ---- drill-down modals ----
  const modal = $("#modal");
  const showModal = (title, html) => {
    $("#modal-title").textContent = title;
    $("#modal-content").innerHTML = html;
    modal.classList.add("open");
  };
  const modalClose = $("#modal-close");
  if (modalClose) modalClose.addEventListener("click", () => modal.classList.remove("open"));
  if (modal) modal.addEventListener("click", e => { if (e.target === modal) modal.classList.remove("open"); });

  const showRid = async rid => {
    const d = await fetch(`/api/rid/${rid}`).then(r=>r.json());
    if (d.error) return showModal(`RID ${rid}`, `<div class="dim">no data</div>`);
    const callsHtml = `<div class="mini-feed">${d.calls.map(c => `<div class="row ${c.encrypted?"enc":""}">
      <span class="ts">${fmtTs(c.ts)}</span>
      <span class="tg click-tg" data-tg="${c.tgid||""}">TG ${c.tgid||""}</span>
      <span>${fmtDur(c.duration_ms)}</span>
      <span>${esc(c.transcript||"")}</span>
    </div>`).join("")||"<em>no calls</em>"}</div>`;
    showModal(`RID ${rid} (${esc(d.info.alias||"")}) on ${esc(d.info.system||"")}`, `
      <h3>PROFILE</h3>
      <div>calls: ${d.info.call_count} &nbsp; airtime: ${(d.info.total_ms/1000).toFixed(1)}s
           &nbsp; first: ${fmtTs(d.info.first_seen)} &nbsp; last: ${fmtTs(d.info.last_seen)}</div>
      <h3>AFFILIATIONS</h3>
      <table class="data"><thead><tr><th>TG</th><th>N</th><th>LAST</th></tr></thead><tbody>
      ${d.affiliations.map(a=>`<tr><td class="click-tg" data-tg="${a.tgid}">${a.tgid}</td><td>${a.n}</td><td>${fmtTs(a.last_ts)}</td></tr>`).join("")}
      </tbody></table>
      <h3>SITES SEEN AT</h3>
      <table class="data"><thead><tr><th>SITE</th><th>N</th><th>LAST</th></tr></thead><tbody>
      ${d.sites.map(s=>`<tr><td>${esc(s.site)}</td><td>${s.n}</td><td>${fmtTs(s.last_ts)}</td></tr>`).join("")}
      </tbody></table>
      <h3>TALKGROUP PARTNERS</h3>
      <table class="data"><thead><tr><th>TG</th><th>CALLS</th></tr></thead><tbody>
      ${d.tg_partners.map(t=>`<tr><td class="click-tg" data-tg="${t.tgid}">${t.tgid}</td><td>${t.n}</td></tr>`).join("")}
      </tbody></table>
      <h3>RECENT CALLS</h3>${callsHtml}`);
  };

  const showTg = async tg => {
    const d = await fetch(`/api/tg/${tg}`).then(r=>r.json());
    if (d.error) return showModal(`TG ${tg}`, `<div class="dim">no data</div>`);
    const encPct = d.enc_ratio?.total ? ((d.enc_ratio.enc/d.enc_ratio.total)*100).toFixed(1) : "0.0";
    const callsHtml = `<div class="mini-feed">${d.calls.map(c => `<div class="row ${c.encrypted?"enc":""}">
      <span class="ts">${fmtTs(c.ts)}</span>
      <span class="rid click-rid" data-rid="${c.source_rid||""}">RID ${c.source_rid||""}</span>
      <span>${fmtDur(c.duration_ms)}</span>
      <span>${esc(c.transcript||"")}</span>
    </div>`).join("")||"<em>no calls</em>"}</div>`;
    showModal(`TG ${tg} (${esc(d.info.alias||"")}) ${esc(d.info.tg_group||"")} on ${esc(d.info.system||"")}`, `
      <h3>PROFILE</h3>
      <div>calls: ${d.info.call_count} &nbsp; airtime: ${(d.info.total_ms/1000).toFixed(1)}s
           &nbsp; priority: ${d.info.priority??"--"} &nbsp; encrypted: ${d.info.encrypted?"YES":"no"}
           &nbsp; enc %: ${encPct}%</div>
      <h3>TOP RIDS</h3>
      <table class="data"><thead><tr><th>RID</th><th>N</th></tr></thead><tbody>
      ${d.top_rids.map(r=>`<tr><td class="click-rid" data-rid="${r.rid}">${r.rid}</td><td>${r.n}</td></tr>`).join("")}
      </tbody></table>
      <h3>RECENT CALLS</h3>${callsHtml}`);
  };

  const showSite = async sid => {
    const d = await fetch(`/api/site/${sid}`).then(r=>r.json());
    if (d.error) return showModal(`site`, `<div class="dim">no data</div>`);
    showModal(`SITE ${esc(d.info.name||d.info.site_id||"")} on ${esc(d.info.system||"")}`, `
      <h3>PROFILE</h3>
      <div>site id: ${esc(d.info.site_id||"--")} &nbsp; first: ${fmtTs(d.info.first_seen)}
           &nbsp; last: ${fmtTs(d.info.last_seen)}</div>
      <h3>TOP RADIOS HERE</h3>
      <table class="data"><thead><tr><th>RID</th><th>N</th><th>LAST</th></tr></thead><tbody>
      ${d.top_rids.map(r=>`<tr><td class="click-rid" data-rid="${r.rid}">${r.rid}</td><td>${r.n}</td><td>${fmtTs(r.last_ts)}</td></tr>`).join("")||'<tr><td colspan="3"><em>none</em></td></tr>'}
      </tbody></table>
      <h3>TOP TALKGROUPS HERE</h3>
      <table class="data"><thead><tr><th>TG</th><th>N</th><th>LAST</th></tr></thead><tbody>
      ${d.top_tgs.map(t=>`<tr><td class="click-tg" data-tg="${t.tgid}">${t.tgid}</td><td>${t.n}</td><td>${fmtTs(t.last_ts)}</td></tr>`).join("")||'<tr><td colspan="3"><em>none</em></td></tr>'}
      </tbody></table>`);
  };

  const showSystem = async sid => {
    const d = await fetch(`/api/system/${sid}`).then(r=>r.json());
    if (d.error) return showModal(`system`, `<div class="dim">no data</div>`);
    showModal(`SYSTEM ${esc(d.info.name||"")}`, `
      <h3>PROFILE</h3>
      <div>protocol: ${esc(d.info.protocol||"--")} &nbsp; talkgroups: ${d.counts.talkgroups}
           &nbsp; radios: ${d.counts.radios} &nbsp; calls: ${d.counts.calls}
           &nbsp; events: ${d.counts.events} &nbsp; encrypted calls: ${d.counts.encrypted_calls}</div>
      <h3>TOP TALKGROUPS</h3>
      <table class="data"><thead><tr><th>TG</th><th>ALIAS</th><th>CALLS</th><th>ENC</th></tr></thead><tbody>
      ${d.top_tgs.map(t=>`<tr><td class="click-tg" data-tg="${t.tgid}">${t.tgid}</td><td>${esc(t.alias||"")}</td><td>${t.call_count}</td><td>${t.encrypted?"YES":""}</td></tr>`).join("")||'<tr><td colspan="4"><em>none</em></td></tr>'}
      </tbody></table>`);
  };

  // ---- inline alias editing ----
  // Alias cells render their content (link or text) plus a dim "edit"
  // affordance; clicking swaps in an input. Enter saves, Esc/blur cancels.
  const aliasCell = (kind, system, id, inner) =>
    `<span class="alias-view">${inner}</span> ` +
    `<span class="edit-alias" data-kind="${kind}" data-system="${esc(system||"")}" data-id="${id}">edit</span>`;
  const startAliasEdit = span => {
    const cell = span.parentElement;
    const view = cell.querySelector(".alias-view");
    if (cell.querySelector(".alias-input")) return;   // already editing
    const input = document.createElement("input");
    input.className = "alias-input";
    input.value = view ? view.textContent.trim() : "";
    if (view) view.style.display = "none";
    span.style.display = "none";
    span.before(input);
    input.focus();
    input.select();
    const cancel = () => { input.remove(); if (view) view.style.display = ""; span.style.display = ""; };
    input.addEventListener("keydown", async ev => {
      if (ev.key === "Escape") { ev.preventDefault(); cancel(); return; }
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      input.disabled = true;
      const r = await fetch("/api/alias", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({kind: span.dataset.kind, system: span.dataset.system,
                              id: parseInt(span.dataset.id, 10), alias: input.value}),
      }).then(r=>r.json()).catch(() => ({error: "network"}));
      if (!r.ok) { input.disabled = false; input.classList.add("err"); input.title = r.error || "save failed"; return; }
      // reload the enclosing tab so every join picks up the new alias
      const panel = cell.closest(".tab-panel");
      if (panel?.id === "tab-directory")      loadDirectory();
      else if (panel?.id === "tab-who")       { loadWho(); loadPresences(); }
      else cancel();
    });
    input.addEventListener("blur", () => { if (!input.disabled) cancel(); });
  };

  document.addEventListener("click", async e => {
    const editSpan = e.target.closest?.(".edit-alias");
    if (editSpan) { startAliasEdit(editSpan); return; }
    const topoSpan = e.target.closest?.(".topo-link");
    if (topoSpan) {
      $(`nav .tab[data-tab="topology"]`)?.click();
      // graph builds async on first visit -- retry the focus until it's up
      const q = topoSpan.dataset.topo || "";
      let tries = 0;
      const tryFocus = () => { if (graphNet) topoSearch(q); else if (++tries < 40) setTimeout(tryFocus, 250); };
      setTimeout(tryFocus, 100);
      return;
    }
    const listenTg = e.target.dataset?.listenTg;
    if (listenTg) { setMonitor(listenTg, e.target.dataset.listenName || "TG " + listenTg, e.target.dataset.listenFreq); return; }
    const rid = e.target.dataset?.rid;
    const tg  = e.target.dataset?.tg;
    if (rid && e.target.classList.contains("click-rid")) showRid(rid);
    if (tg && e.target.classList.contains("click-tg"))   showTg(tg);
  });

  // Tab lazy loaders
  const loaded = {};
  const onTabShown = t => {
    if (t === "directory")            { loadDirectory(); }
    if (t === "network")              { loadSites(); loadEnc(); loadDenies(); loadAffs(); }
    if (t === "tx")                   { loadTxFilters(); loadTX(); }
    if (t === "tuners")               { loadTuners(); }
    if (t === "who")                  { loadWho(); loadPresences(); }
    if (t === "stream")               { loadTgAliasMap(); }
    if (t === "alerts")               { loadAlerts(); }
    if (t === "topology")             { loadGraph(); }
    if (t === "stats")                { loadStats(); }
    if (t === "live")                 { loadLive();  }
  };

  // Boot
  fetch("/api/config").then(r => r.json()).then(cfg => {
    Object.assign(RR_SIDS, cfg.rr_sids || {});
    CANON_RULES = cfg.canonical_rules || [];
  }).catch(() => {});
  refreshSummary();
  loadLive();
  $$("table.data").forEach(makeSortable);
  setInterval(refreshSummary, 5000);
  setInterval(() => { if (whoTabActive()) loadWho(); }, 10000);
  setInterval(() => { if (tabActive("tuners")) loadTuners(); }, 10000);
})();
