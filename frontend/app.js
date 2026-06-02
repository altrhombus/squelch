/* Squelch frontend — vanilla JS, no build step */

const BAND_RANGES = {
  fm:      { min: 87.5,   max: 108.0, step: 0.1,    unit: "MHz" },
  hd:      { min: 87.5,   max: 108.0, step: 0.1,    unit: "MHz" },
  am:      { min: 530,    max: 1700,  step: 10,     unit: "kHz" },
  scanner: { min: 25,     max: 1300,  step: 0.025,  unit: "MHz" },
};

let currentBand = "fm";
let currentFreq = 91.1;
let isPlaying = false;
let isRecording = false;
let ws = null;
let wsReconnectTimer = null;

// Art / metadata state
let _prevHasArt = false;
let _prevArtUrl = "";
let _prevHdLocked = false;
let _currentAppleMusicUrl = null;

// History auto-refresh
let _prevTrackKey = "";
let _historyRefreshTimer = null;

// ---------------------------------------------------------------------------
// Elements
// ---------------------------------------------------------------------------

const player     = document.getElementById("player");
const btnPlay    = document.getElementById("btn-play");
const iconPlay   = document.getElementById("icon-play");
const iconPause  = document.getElementById("icon-pause");
const btnRecord  = document.getElementById("btn-record");
const volume     = document.getElementById("volume");
const dial       = document.getElementById("dial");
const dialTicks  = document.getElementById("dial-ticks");
const freqValue  = document.getElementById("freq-value");
const freqUnit   = document.getElementById("freq-unit");
const freqInput  = document.getElementById("freq-input");
const btnGo      = document.getElementById("btn-go");
const presetsList    = document.getElementById("presets-list");
const recordingsList = document.getElementById("recordings-list");
const historyList    = document.getElementById("history-list");

const elStationName   = document.getElementById("station-name");
const elStationSlogan = document.getElementById("station-slogan");
const elTrackInfo     = document.getElementById("track-info");
const elArt           = document.getElementById("art");
const artBack         = document.getElementById("art-back");
const artBlurBg       = document.getElementById("art-blur-bg");
const elArtWrap       = document.getElementById("art-wrap");
const elHdBadge       = document.getElementById("hd-badge");
const elPtyBadge      = document.getElementById("pty-badge");
const bars            = document.querySelectorAll(".bar");

const modalPreset     = document.getElementById("modal-preset");
const presetNameInput = document.getElementById("preset-name-input");
const btnSavePreset   = document.getElementById("btn-save-preset");
const btnPresetCancel = document.getElementById("btn-preset-cancel");
const btnPresetSave   = document.getElementById("btn-preset-save");

// ---------------------------------------------------------------------------
// Band / dial setup
// ---------------------------------------------------------------------------

function setBand(band) {
  currentBand = band;
  const r = BAND_RANGES[band];

  document.querySelectorAll(".band-tab").forEach(t => {
    const active = t.dataset.band === band;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", active);
  });

  dial.min = r.min;
  dial.max = r.max;
  dial.step = r.step;
  freqUnit.textContent = r.unit;
  freqInput.step = r.step;
  freqInput.min = r.min;
  freqInput.max = r.max;
  freqInput.placeholder = `${r.min}–${r.max} ${r.unit}`;

  buildDialTicks(r);
  setFreq(clamp(currentFreq, r.min, r.max));
}

function buildDialTicks(r) {
  dialTicks.innerHTML = "";
  const count = 6;
  for (let i = 0; i <= count; i++) {
    const v = r.min + ((r.max - r.min) * i / count);
    const s = document.createElement("span");
    s.textContent = formatFreq(v, currentBand);
    dialTicks.appendChild(s);
  }
}

function setFreq(f) {
  currentFreq = f;
  dial.value = f;
  freqValue.textContent = formatFreq(f, currentBand);
  freqInput.value = f;
}

function formatFreq(f, band) {
  if (band === "am") return Math.round(f);
  return parseFloat(f).toFixed(band === "scanner" ? 3 : 1);
}

function clamp(v, min, max) {
  return Math.min(Math.max(Number(v), min), max);
}

// ---------------------------------------------------------------------------
// Tuning
// ---------------------------------------------------------------------------

async function tune(freq, band) {
  band = band || currentBand;
  setFreq(freq);
  setBandIfChanged(band);

  // Start/reconnect the audio stream synchronously inside the user gesture.
  // Calling play() here (before any await) satisfies Safari's requirement
  // that play() be invoked from a user-gesture call stack.
  _startStream();

  const res = await api("POST", "/tune", {
    frequency: freq,
    band: band,
    gain: "auto",
    stereo_mode: "auto",
  });

  if (res.error) {
    elStationName.textContent = "Error";
  }
}

function _startStream() {
  player.src = "/stream";
  player.muted = true;
  player.play()
    .then(() => { player.muted = false; setPlayState(true); })
    .catch(err => {
      player.muted = false;
      if (err?.name === "NotAllowedError") {
        elTrackInfo.textContent = "Tap ▶ to start";
        elTrackInfo.classList.add("muted");
      }
    });
}

function setBandIfChanged(band) {
  if (band !== currentBand) setBand(band);
}

function setPlayState(playing) {
  isPlaying = playing;
  iconPlay.classList.toggle("hidden", playing);
  iconPause.classList.toggle("hidden", !playing);
  btnPlay.setAttribute("aria-pressed", playing);
  btnPlay.setAttribute("aria-label", playing ? "Pause" : "Play");
}

// ---------------------------------------------------------------------------
// Frequency step buttons (with long-press continuous stepping)
// ---------------------------------------------------------------------------

function setupStepButton(btn, direction) {
  let holdTimer = null;
  let holdInterval = null;

  function stepOne() {
    const r = BAND_RANGES[currentBand];
    const raw = currentFreq + direction * r.step;
    setFreq(clamp(parseFloat(raw.toFixed(4)), r.min, r.max));
  }

  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    stepOne(); // immediate first step on every tap
    holdTimer = setTimeout(() => {
      holdInterval = setInterval(stepOne, 130);
    }, 450);
  });

  const commit = () => {
    clearTimeout(holdTimer);
    clearInterval(holdInterval);
    holdTimer = null;
    holdInterval = null;
    tune(currentFreq);
  };

  btn.addEventListener("pointerup", commit);
  btn.addEventListener("pointercancel", () => { clearTimeout(holdTimer); clearInterval(holdInterval); });
  btn.addEventListener("pointerleave", () => { clearTimeout(holdTimer); clearInterval(holdInterval); });

  btn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); stepOne(); tune(currentFreq); }
  });
}

// ---------------------------------------------------------------------------
// WebSocket — live metadata
// ---------------------------------------------------------------------------

function connectWs() {
  if (ws) ws.close();
  clearTimeout(wsReconnectTimer);

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    document.getElementById("ws-status")?.classList.add("hidden");
  };

  ws.onmessage = (e) => {
    try { applyMeta(JSON.parse(e.data)); } catch {}
  };

  ws.onclose = () => {
    document.getElementById("ws-status")?.classList.remove("hidden");
    wsReconnectTimer = setTimeout(connectWs, 3000);
  };
}

// ---------------------------------------------------------------------------
// Art crossfade
// ---------------------------------------------------------------------------

function updateArt(rawUrl) {
  const hasNewArt = rawUrl !== "/static/placeholder.svg";
  const src = hasNewArt ? rawUrl + "?t=" + Date.now() : "/static/placeholder.svg";

  artBack.src = elArt.src || "/static/placeholder.svg";
  elArt.style.opacity = "0";
  artBlurBg.style.backgroundImage = hasNewArt ? `url("${src}")` : "none";

  const img = new Image();
  img.onload = () => {
    elArt.src = img.src;
    elArt.style.opacity = "1";
  };
  img.onerror = () => {
    elArt.src = "/static/placeholder.svg";
    elArt.style.opacity = "1";
    artBlurBg.style.backgroundImage = "none";
  };
  img.src = src;
}

// ---------------------------------------------------------------------------
// Metadata from WebSocket
// ---------------------------------------------------------------------------

function applyMeta(m) {
  // Station name
  if (m.station_name) {
    elStationName.textContent = m.station_name;
    document.title = m.station_name + " — Squelch";
  } else if (m.frequency && m.band && m.state !== "idle") {
    const unit = m.band === "am" ? "kHz" : "MHz";
    const freq = m.band === "am"
      ? Math.round(m.frequency / 1e3)
      : parseFloat(m.frequency / 1e6).toFixed(1);
    elStationName.textContent = `${freq} ${unit}`;
    document.title = `${freq} ${unit} — Squelch`;
  } else {
    elStationName.textContent = "Squelch";
    document.title = "Squelch";
  }

  // Station slogan (HD Radio)
  if (m.slogan) {
    elStationSlogan.textContent = m.slogan;
    elStationSlogan.classList.remove("hidden");
  } else {
    elStationSlogan.classList.add("hidden");
  }

  // Track info line
  if (m.artist && m.title) {
    elTrackInfo.textContent = `${m.artist} — ${m.title}`;
    elTrackInfo.classList.remove("muted");
  } else if (m.title) {
    elTrackInfo.textContent = m.title;
    elTrackInfo.classList.remove("muted");
  } else {
    const hint = {
      idle:      "Ready to tune",
      tuning:    "Tuning…",
      buffering: "Buffering…",
      live:      m.band === "fm" ? "Waiting for RDS…" : "Live",
    }[m.state] || "";
    elTrackInfo.textContent = hint;
    elTrackInfo.classList.add("muted");
  }

  // History auto-refresh when track changes
  const trackKey = `${m.artist || ""}|${m.title || ""}`;
  if (trackKey !== _prevTrackKey && (m.artist || m.title)) {
    _prevTrackKey = trackKey;
    clearTimeout(_historyRefreshTimer);
    _historyRefreshTimer = setTimeout(loadHistory, 6000);
  }

  // Cover art — crossfade only when has_art or art_url changes
  const nowHasArt = !!(m.has_art && m.art_url);
  if (nowHasArt !== _prevHasArt || (nowHasArt && m.art_url !== _prevArtUrl)) {
    _prevHasArt = nowHasArt;
    _prevArtUrl = m.art_url || "";
    updateArt(nowHasArt ? m.art_url : "/static/placeholder.svg");
  }

  // Apple Music link on album art
  _currentAppleMusicUrl = m.apple_music_url || null;
  updateArtLink(m);

  // HD badge with lock-in pulse animation
  if (m.hd_locked && !_prevHdLocked) {
    elHdBadge.classList.add("hd-pulse");
    elHdBadge.addEventListener("animationend", () => elHdBadge.classList.remove("hd-pulse"), { once: true });
  }
  _prevHdLocked = !!m.hd_locked;
  toggleEl(elHdBadge, m.hd_locked);

  // PTY badge
  if (m.pty) {
    elPtyBadge.textContent = m.pty;
    elPtyBadge.classList.remove("hidden");
  } else {
    elPtyBadge.classList.add("hidden");
  }

  // Signal bars
  const b = m.signal_bars || 0;
  const signalLabels = ["No signal", "Poor", "Weak", "Fair", "Good", "Excellent"];
  const signalLabel = signalLabels[b] || "No signal";
  const signalMeter = document.getElementById("signal-meter");
  if (signalMeter) {
    signalMeter.setAttribute("aria-label", `Signal: ${signalLabel}`);
    signalMeter.dataset.signalLabel = signalLabel;
  }
  bars.forEach(bar => bar.classList.toggle("active", Number(bar.dataset.n) <= b));

  // Diagnostics panel (elements may not exist in HTML — null-safe)
  if (m.diag) applyDiag(m.diag, m.band);

  // Post-processing meter panel
  applyPpState(m.post_processing, m.band);
}

function updateArtLink(m) {
  // Prefer the direct Apple Music URL from the iTunes lookup.
  // Fall back to an Apple Music search URL when artist+title are available.
  let url = _currentAppleMusicUrl;
  if (!url && m.artist && m.title) {
    url = `https://music.apple.com/search?term=${encodeURIComponent(m.artist + " " + m.title)}`;
  }
  if (url) {
    elArtWrap.classList.add("has-link");
    elArtWrap.onclick = () => window.open(url, "_blank", "noopener");
  } else {
    elArtWrap.classList.remove("has-link");
    elArtWrap.onclick = null;
  }
}

function toggleEl(el, show) {
  el.classList.toggle("hidden", !show);
}

// ---------------------------------------------------------------------------
// Post-processing meter panel
// ---------------------------------------------------------------------------

let _ppBypass = false;

function applyPpState(pp, band) {
  const panel = document.getElementById("pp-panel");
  if (!panel) return;

  // Only show for FM band when post-processing is enabled
  if (!pp || !pp.enabled || band !== "fm") {
    panel.classList.add("hidden");
    return;
  }

  const bypassed   = pp.bypass || _ppBypass;
  const signalPct  = pp.signal_pct ?? 100;
  const mods       = pp.modules || {};
  const anyActive  = !bypassed && Object.values(mods).some(m => m.active);

  panel.classList.remove("hidden");

  // Header color: amber when actively enhancing, dimmed when bypassed or clean signal
  const header = document.getElementById("pp-header");
  if (header) {
    header.classList.toggle("pp-active",  anyActive);
    header.classList.toggle("pp-bypassed", bypassed);
  }

  const label = document.getElementById("pp-header-label");
  if (label) label.textContent = bypassed ? "BYPASSED" : "ENHANCED";

  const bypassBtn = document.getElementById("btn-pp-bypass");
  if (bypassBtn) {
    bypassBtn.textContent = bypassed ? "Resume" : "Bypass";
    bypassBtn.setAttribute("aria-pressed", String(bypassed));
  }

  // Signal bar
  _setPpBar("signal", signalPct, 100,
    v => v > 80 ? "good" : v > 40 ? "fair" : "weak",
    v => v + "%");

  // Module rows
  const ms = mods.ms || {};
  _setPpModuleRow("ms", ms.active && !bypassed, "active", "off");

  const comp = mods.compress || {};
  _setPpModuleRow("comp", comp.active && !bypassed, "active",
    "off", comp.gr_db != null ? comp.gr_db.toFixed(1) + " dB" : null);

  const exc = mods.exciter || {};
  _setPpModuleRow("exc", exc.active && !bypassed, "active", "off");

  const cn = mods.comfort_noise || {};
  _setPpModuleRow("cn", cn.active && !bypassed, "active", "off");
}

function _setPpBar(id, value, maxVal, colorFn, labelFn) {
  const bar = document.getElementById(`pp-bar-${id}`);
  const val = document.getElementById(`pp-val-${id}`);
  if (!bar || !val) return;
  const pct = Math.min(100, Math.max(0, (value / maxVal) * 100));
  bar.style.width = pct + "%";
  bar.className   = "pp-bar " + colorFn(value);
  val.textContent = labelFn(value);
}

function _setPpModuleRow(id, active, activeLabel, offLabel, extraLabel) {
  const bar = document.getElementById(`pp-bar-${id}`);
  const val = document.getElementById(`pp-val-${id}`);
  if (!bar || !val) return;
  bar.style.width = active ? "100%" : "0%";
  bar.className   = "pp-bar " + (active ? "good" : "inactive");
  val.textContent = active ? (extraLabel || activeLabel) : offLabel;
}

// Collapse/expand panel body
document.getElementById("pp-header")?.addEventListener("click", _togglePpPanel);
document.getElementById("pp-header")?.addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); _togglePpPanel(); }
});

function _togglePpPanel() {
  const body    = document.getElementById("pp-body");
  const header  = document.getElementById("pp-header");
  const chevron = document.getElementById("pp-chevron");
  if (!body) return;
  const open = !body.classList.contains("hidden");
  body.classList.toggle("hidden", open);
  header?.setAttribute("aria-expanded", String(!open));
  if (chevron) chevron.innerHTML = open ? "&#9660;" : "&#9650;";
}

document.getElementById("btn-pp-bypass")?.addEventListener("click", async (e) => {
  e.stopPropagation();   // don't trigger panel expand
  _ppBypass = !_ppBypass;
  await api("POST", "/post-processing/bypass", { bypass: _ppBypass });
});

// ---------------------------------------------------------------------------
// Diagnostics panel (null-safe — elements may be absent from HTML)
// ---------------------------------------------------------------------------

let _lastDiag = {};

function applyDiag(d, band) {
  _lastDiag = { ...d, band };

  setDiagMeter("iq", d.iq_rms ?? 0, 0.7,
    v => v > 0.45 ? "weak" : v > 0.1 ? "good" : "fair",
    v => v.toFixed(3));

  const compRow = document.getElementById("diag-comp-row");
  if (compRow) compRow.style.display = (band === "fm" || !band) ? "" : "none";
  setDiagMeter("comp", d.composite_rms ?? 0, 0.6,
    v => v > 0.2 ? "good" : v > 0.05 ? "fair" : "weak",
    v => v.toFixed(3));

  const pilotRow = document.getElementById("diag-pilot-row");
  if (pilotRow) pilotRow.style.display = (band === "fm" || !band) ? "" : "none";
  setDiagMeter("pilot", d.pilot_rms ?? 0, 0.10,
    v => v > 0.06 ? "good" : v > 0.02 ? "fair" : "weak",
    v => v.toFixed(4));

  const noiseRow = document.getElementById("diag-noise-row");
  if (noiseRow) noiseRow.style.display = (band === "fm" || !band) ? "" : "none";
  setDiagMeter("noise", d.noise_rms ?? 0, 0.10,
    v => v < 0.02 ? "good" : v < 0.05 ? "fair" : "weak",
    v => v.toFixed(4));

  const blendRow = document.getElementById("diag-blend-row");
  if (blendRow) blendRow.style.display = (band === "fm" || !band) ? "" : "none";
  setDiagMeter("blend", d.blend ?? 0, 1.0,
    v => v > 0.6 ? "good" : v > 0.2 ? "fair" : "weak",
    v => Math.round(v * 100) + "%");

  setDiagMeter("audio", d.audio_rms ?? 0, 0.5,
    v => v > 0.05 && v < 0.45 ? "good" : v >= 0.45 ? "weak" : "fair",
    v => v.toFixed(3));

  const gainRow = document.getElementById("diag-gain-row");
  if (gainRow) {
    const hasGain = d.gain_db != null;
    gainRow.style.display = (hasGain && (band === "fm" || !band)) ? "" : "none";
    if (hasGain) {
      setDiagMeter("gain", d.gain_db, 50,
        v => v <= 35 ? "good" : v <= 42 ? "fair" : "weak",
        v => v.toFixed(1) + " dB");
    }
  }
}

function setDiagMeter(id, value, maxVal, colorFn, labelFn) {
  const bar = document.getElementById(`diag-${id}-bar`);
  const val = document.getElementById(`diag-${id}-val`);
  if (!bar || !val) return;
  const pct = Math.min(100, (value / maxVal) * 100);
  bar.style.width = pct + "%";
  bar.className = "diag-fill " + colorFn(value);
  val.textContent = value > 0 ? labelFn(value) : "—";
}

document.getElementById("btn-copy-diag")?.addEventListener("click", () => {
  const info = {
    timestamp: new Date().toISOString(),
    frequency: currentFreq,
    band: currentBand,
    ...(_lastDiag || {}),
  };
  const text = JSON.stringify(info, null, 2);
  const btn = document.getElementById("btn-copy-diag");
  const orig = btn.textContent;
  const finish = () => {
    btn.textContent = "Copied!";
    setTimeout(() => btn.textContent = orig, 1500);
  };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(finish).catch(() => fallbackCopy(text, finish));
  } else {
    fallbackCopy(text, finish);
  }
});

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand("copy"); done(); } catch (_) {}
  document.body.removeChild(ta);
}

// ---------------------------------------------------------------------------
// Generic API helper
// ---------------------------------------------------------------------------

async function api(method, path, body) {
  try {
    const opts = { method, headers: {} };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json().catch(() => ({}));
  } catch (e) {
    console.error(method, path, e);
    return { error: e.message };
  }
}

// ---------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------

async function loadPresets() {
  const presets = await api("GET", "/presets");
  presetsList.innerHTML = "";
  if (!Array.isArray(presets) || !presets.length) {
    presetsList.innerHTML = '<p class="empty-hint">No presets yet</p>';
    return;
  }
  presets.forEach(p => {
    const item = document.createElement("div");
    item.className = "preset-item";
    item.innerHTML = `
      <span class="preset-freq">${p.frequency} ${BAND_RANGES[p.band]?.unit || ""}</span>
      <span class="preset-name">${esc(p.name)}</span>
      <span class="preset-band">${p.band}</span>
      <button class="btn-delete" aria-label="Delete preset ${esc(p.name)}" title="Delete">×</button>
    `;
    item.querySelector(".btn-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (confirm(`Delete "${p.name}"?`)) {
        await api("DELETE", `/presets/${p.id}`);
        loadPresets();
      }
    });
    item.addEventListener("click", (e) => {
      if (!e.target.closest(".btn-delete")) tune(p.frequency, p.band);
    });
    presetsList.appendChild(item);
  });
}

async function saveCurrentPreset(name) {
  await api("POST", "/presets", {
    name,
    frequency: currentFreq,
    band: currentBand,
    gain: "auto",
    bandwidth: "wide",
    stereo_mode: "auto",
  });
  loadPresets();
}

// ---------------------------------------------------------------------------
// Recordings
// ---------------------------------------------------------------------------

async function loadRecordings() {
  const recs = await api("GET", "/recordings");
  recordingsList.innerHTML = "";
  if (!Array.isArray(recs) || !recs.length) {
    recordingsList.innerHTML = '<p class="empty-hint">No recordings yet</p>';
    return;
  }
  recs.forEach(r => {
    const item = document.createElement("div");
    item.className = "recording-item";
    const dur = r.duration_seconds ? formatDuration(r.duration_seconds) : "–";
    const label = r.station_name
      ? `${r.station_name}${r.title ? " – " + r.title : ""}`
      : r.filename;
    item.innerHTML = `
      <span class="recording-name" title="${esc(r.filename)}">${esc(label)}</span>
      <span class="recording-dur">${dur}</span>
      <button class="btn-icon btn-play-rec" aria-label="Play ${esc(label)}">
        <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <a class="btn-small" href="/recordings/${r.id}/download" download="${esc(r.filename)}" aria-label="Download ${esc(label)}">↓</a>
      <button class="btn-delete" aria-label="Delete ${esc(label)}" title="Delete">×</button>
    `;
    item.querySelector(".btn-play-rec").addEventListener("click", (e) => {
      e.stopPropagation();
      playRecording(r.id, label);
    });
    item.querySelector(".btn-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (confirm("Delete this recording?")) {
        await api("DELETE", `/recordings/${r.id}`);
        loadRecordings();
      }
    });
    recordingsList.appendChild(item);
  });
}

function playRecording(id, label) {
  player.src = `/recordings/${id}/download`;
  player.play().then(() => setPlayState(true)).catch(() => {});
  elStationName.textContent = label || "Recording";
  document.title = (label || "Recording") + " — Squelch";
  elTrackInfo.textContent = "Playing recording";
  elTrackInfo.classList.remove("muted");
}

function formatDuration(s) {
  const m = Math.floor(s / 60), sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

async function loadHistory() {
  const items = await api("GET", "/history");
  historyList.innerHTML = "";
  if (!Array.isArray(items) || !items.length) {
    historyList.innerHTML = '<p class="empty-hint">Nothing heard yet</p>';
    return;
  }
  items.forEach(h => {
    const freq = h.band === "am"
      ? `${Math.round(h.frequency / 1e3)} kHz`
      : `${parseFloat(h.frequency / 1e6).toFixed(1)} MHz`;
    const musicUrl = (h.artist && h.title)
      ? `https://music.apple.com/search?term=${encodeURIComponent(h.artist + " " + h.title)}`
      : null;
    // Only show freq badge separately when we also have a station name
    const freqBadge = h.station_name
      ? `<span class="history-freq">${freq}</span>`
      : "";

    const row = document.createElement("div");
    row.className = "history-row";
    row.innerHTML = `
      <div class="history-swipe-bg" aria-hidden="true">Delete</div>
      <div class="history-item">
        <div class="history-info">
          <div class="history-line1">
            <div class="history-name-freq">
              <span class="history-station">${esc(h.station_name || freq)}</span>
              ${freqBadge}
            </div>
            <span class="history-time">${timeAgo(h.seen_at)}</span>
          </div>
          ${h.artist && h.title
            ? `<span class="history-track">${esc(h.artist)} — ${esc(h.title)}</span>`
            : ""}
        </div>
        ${musicUrl ? `<a class="btn-music" href="${musicUrl}" target="_blank" rel="noopener" aria-label="Open in Apple Music" title="Open in Apple Music">♫</a>` : ""}
        <button class="btn-delete btn-history-del" aria-label="Delete history item" title="Delete">×</button>
      </div>
    `;

    const item = row.querySelector(".history-item");

    // Click to retune (suppress if swipe or music link)
    item.addEventListener("click", (e) => {
      if (e.target.closest(".btn-music") || e.target.closest(".btn-history-del")) return;
      if (item.dataset.swiped) { item.dataset.swiped = ""; return; }
      const tuneFreq = h.band === "am"
        ? Math.round(h.frequency / 1e3)
        : parseFloat(h.frequency / 1e6);
      tune(tuneFreq, h.band);
    });

    // Desktop delete button
    row.querySelector(".btn-history-del").addEventListener("click", (e) => {
      e.stopPropagation();
      _deleteHistoryRow(h.id, row);
    });

    _addSwipeDelete(row, item, h.id);
    historyList.appendChild(row);
  });
}

function _addSwipeDelete(row, item, id) {
  const bg = row.querySelector(".history-swipe-bg");
  const REVEAL = 72;
  let x0 = 0, y0 = 0;
  let determined = false, isHoriz = false;
  let revealed = false;

  const snap = (open) => {
    item.style.transition = "transform 0.2s ease";
    item.style.transform = open ? `translateX(-${REVEAL}px)` : "translateX(0)";
    bg.style.pointerEvents = open ? "auto" : "none";
    revealed = open;
  };

  item.addEventListener("touchstart", (e) => {
    x0 = e.touches[0].clientX;
    y0 = e.touches[0].clientY;
    determined = false;
    isHoriz = false;
    item.style.transition = "none";
  }, { passive: true });

  item.addEventListener("touchmove", (e) => {
    const dx = e.touches[0].clientX - x0;
    const dy = e.touches[0].clientY - y0;
    if (!determined && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
      determined = true;
      isHoriz = Math.abs(dx) > Math.abs(dy);
    }
    if (!isHoriz) return;
    e.preventDefault();
    item.dataset.swiped = "1";
    const clamped = revealed
      ? Math.max(-REVEAL, Math.min(0, dx - REVEAL))
      : Math.max(-REVEAL, Math.min(0, dx));
    item.style.transform = `translateX(${clamped}px)`;
  }, { passive: false });

  item.addEventListener("touchend", (e) => {
    if (!isHoriz) return;
    const dx = e.changedTouches[0].clientX - x0;
    const netDx = revealed ? dx - REVEAL : dx;
    snap(netDx < -(REVEAL / 2));
    setTimeout(() => { item.dataset.swiped = ""; }, 50);
  });

  bg.addEventListener("click", (e) => {
    e.stopPropagation();
    _deleteHistoryRow(id, row);
  });
}

async function _deleteHistoryRow(id, row) {
  const h = row.offsetHeight;
  row.style.height = h + "px";
  row.style.overflow = "hidden";
  row.style.transition = "height 0.2s ease, opacity 0.15s ease";
  requestAnimationFrame(() => {
    row.style.height = "0";
    row.style.opacity = "0";
  });
  await api("DELETE", `/history/${id}`);
  setTimeout(() => {
    row.remove();
    if (!historyList.querySelector(".history-row")) {
      historyList.innerHTML = '<p class="empty-hint">Nothing heard yet</p>';
    }
  }, 220);
}

function timeAgo(ts) {
  if (!ts) return "";
  // seen_at is a Unix epoch integer (seconds). Plain arithmetic, no parsing.
  const diff = Date.now() / 1000 - Number(ts);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// ---------------------------------------------------------------------------
// Modal — save preset (with focus trap)
// ---------------------------------------------------------------------------

let _modalFocusReturn = null;
const FOCUSABLE_SEL = 'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

function openModal() {
  _modalFocusReturn = document.activeElement;
  modalPreset.classList.remove("hidden");
  presetNameInput.focus();
  modalPreset.addEventListener("keydown", _trapFocus);
}

function closeModal() {
  modalPreset.classList.add("hidden");
  modalPreset.removeEventListener("keydown", _trapFocus);
  _modalFocusReturn?.focus();
}

function _trapFocus(e) {
  if (e.key === "Escape") { e.preventDefault(); closeModal(); return; }
  if (e.key !== "Tab") return;
  const els = Array.from(modalPreset.querySelectorAll(FOCUSABLE_SEL));
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
}

// ---------------------------------------------------------------------------
// XSS-safe HTML escaping
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

// Signal strength tooltip
const signalTooltipEl = document.getElementById("signal-tooltip");
const signalMeterEl   = document.getElementById("signal-meter");
signalMeterEl?.addEventListener("mouseenter", () => {
  if (!signalTooltipEl) return;
  const label = signalMeterEl.dataset.signalLabel || "No signal";
  signalTooltipEl.textContent = `Signal: ${label}`;
  const rect = signalMeterEl.getBoundingClientRect();
  signalTooltipEl.style.left = `${rect.left + rect.width / 2}px`;
  signalTooltipEl.style.top  = `${rect.top}px`;
  signalTooltipEl.classList.add("visible");
});
signalMeterEl?.addEventListener("mouseleave", () => {
  signalTooltipEl?.classList.remove("visible");
});

// Band tabs
document.querySelectorAll(".band-tab").forEach(tab => {
  tab.addEventListener("click", () => setBand(tab.dataset.band));
});

// Frequency step buttons
setupStepButton(document.getElementById("btn-step-down"), -1);
setupStepButton(document.getElementById("btn-step-up"),   +1);

// Dial scrub (preview) then commit on release
dial.addEventListener("input", () => setFreq(parseFloat(dial.value)));
dial.addEventListener("change", () => tune(currentFreq));

// Manual frequency input
btnGo.addEventListener("click", () => {
  const v = parseFloat(freqInput.value);
  if (!isNaN(v)) tune(clamp(v, BAND_RANGES[currentBand].min, BAND_RANGES[currentBand].max));
});
freqInput.addEventListener("keydown", e => { if (e.key === "Enter") btnGo.click(); });

// Play/pause
btnPlay.addEventListener("click", () => {
  if (isPlaying) {
    player.pause();
    setPlayState(false);
  } else {
    _startStream();
  }
});

player.addEventListener("play",    () => setPlayState(true));
player.addEventListener("playing", () => setPlayState(true));  // also fires after buffering resumes
player.addEventListener("pause",   () => setPlayState(false));
player.addEventListener("ended",   () => setPlayState(false));
player.addEventListener("error",   () => setPlayState(false));

// Volume
volume.addEventListener("input", () => { player.volume = parseFloat(volume.value); });

// Record
btnRecord.addEventListener("click", async () => {
  if (isRecording) {
    await api("POST", "/record/stop");
    isRecording = false;
    btnRecord.classList.remove("active");
    btnRecord.setAttribute("aria-pressed", "false");
    btnRecord.setAttribute("aria-label", "Start recording");
    loadRecordings();
  } else {
    const res = await api("POST", "/record/start");
    if (!res.error) {
      isRecording = true;
      btnRecord.classList.add("active");
      btnRecord.setAttribute("aria-pressed", "true");
      btnRecord.setAttribute("aria-label", "Stop recording");
    }
  }
});

// Save preset
btnSavePreset.addEventListener("click", () => {
  presetNameInput.value = elStationName.textContent !== "Squelch"
    ? elStationName.textContent : "";
  openModal();
});
btnPresetCancel.addEventListener("click", closeModal);
btnPresetSave.addEventListener("click", async () => {
  const name = presetNameInput.value.trim();
  if (name) {
    await saveCurrentPreset(name);
    closeModal();
  }
});
presetNameInput.addEventListener("keydown", e => { if (e.key === "Enter") btnPresetSave.click(); });
modalPreset.addEventListener("click", e => { if (e.target === modalPreset) closeModal(); });

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

setBand("fm");
connectWs();
loadPresets();
loadRecordings();
loadHistory();

api("GET", "/record/status").then(s => {
  if (s.recording) {
    isRecording = true;
    btnRecord.classList.add("active");
    btnRecord.setAttribute("aria-pressed", "true");
    btnRecord.setAttribute("aria-label", "Stop recording");
  }
});
