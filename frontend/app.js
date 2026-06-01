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
let hlsInstance = null;
let ws = null;
let wsReconnectTimer = null;

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
const presetsList   = document.getElementById("presets-list");
const recordingsList = document.getElementById("recordings-list");

const elStationName = document.getElementById("station-name");
const elTrackInfo   = document.getElementById("track-info");
const elArt         = document.getElementById("art");
const elHdBadge     = document.getElementById("hd-badge");
const elPtyBadge    = document.getElementById("pty-badge");
const elStereo      = document.getElementById("stereo-indicator");
const bars          = document.querySelectorAll(".bar");

const selStereo    = document.getElementById("sel-stereo");
const selBandwidth = document.getElementById("sel-bandwidth");
const selGain      = document.getElementById("sel-gain");
const inputSquelch = document.getElementById("input-squelch");
const squelchVal   = document.getElementById("squelch-val");

const modalPreset       = document.getElementById("modal-preset");
const presetNameInput   = document.getElementById("preset-name-input");
const btnSavePreset     = document.getElementById("btn-save-preset");
const btnPresetCancel   = document.getElementById("btn-preset-cancel");
const btnPresetSave     = document.getElementById("btn-preset-save");

// ---------------------------------------------------------------------------
// Band / dial setup
// ---------------------------------------------------------------------------

function setBand(band) {
  currentBand = band;
  const r = BAND_RANGES[band];

  document.querySelectorAll(".band-tab").forEach(t =>
    t.classList.toggle("active", t.dataset.band === band)
  );

  dial.min = r.min;
  dial.max = r.max;
  dial.step = r.step;
  freqUnit.textContent = r.unit;
  freqInput.step = r.step;
  freqInput.min = r.min;
  freqInput.max = r.max;
  freqInput.placeholder = `${r.min}–${r.max} ${r.unit}`;

  // Quality drawer — only meaningful for FM/HD
  const isAudio = (band === "fm" || band === "hd");
  document.getElementById("quality-drawer").style.display = isAudio ? "" : "none";

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

  const res = await api("POST", "/tune", {
    frequency: freq,
    band: band,
    gain: selGain.value,
    bandwidth: selBandwidth.value,
    stereo_mode: selStereo.value,
  });

  if (!res.error) {
    elStationName.textContent = "Tuning…";
    elTrackInfo.textContent = "";
    // Player will attach when the server pushes "hls_ready" via WebSocket.
    // The fallback timeout covers the case where the WS message is missed.
    clearTimeout(window._hlsFallbackTimer);
    window._hlsFallbackTimer = setTimeout(() => {
      if (!isPlaying) attachPlayer();
    }, 12000);
  }
}

function setBandIfChanged(band) {
  if (band !== currentBand) setBand(band);
}

// ---------------------------------------------------------------------------
// HLS player
// ---------------------------------------------------------------------------

function autoplay() {
  // Muted autoplay is universally allowed. Start muted, unmute immediately in
  // .then() — the transition is in the same microtask so it's inaudible.
  player.muted = true;
  player.play()
    .then(() => {
      player.muted = false;
      setPlayState(true);
    })
    .catch(err => {
      player.muted = false;
      if (err && err.name === "NotAllowedError") {
        elTrackInfo.textContent = "Tap ▶ to start";
        elTrackInfo.classList.add("muted");
      }
    });
}

function attachPlayer() {
  const src = "/hls/stream.m3u8";

  if (hlsInstance) {
    hlsInstance.destroy();
    hlsInstance = null;
  }

  // Detect native HLS support (Safari on macOS/iOS) via canPlayType,
  // not via MediaSource — Safari has both MSE and native HLS.
  const nativeHLS = player.canPlayType("application/vnd.apple.mpegurl") !== "";

  if (nativeHLS) {
    player.src = src;
    autoplay();
  } else if (Hls.isSupported()) {
    hlsInstance = new Hls({
      lowLatencyMode: false,
      maxBufferLength: 30,
      // Retry manifest aggressively — first segment takes ~3-5s to appear
      manifestLoadingMaxRetry: 15,
      manifestLoadingRetryDelay: 1000,
      manifestLoadingMaxRetryTimeout: 8000,
    });
    hlsInstance.loadSource(src);
    hlsInstance.attachMedia(player);
    // Wait for manifest before playing — calling play() before MANIFEST_PARSED
    // results in silent failure because there's no media to play yet.
    hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => autoplay());
    hlsInstance.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) console.warn("HLS fatal error:", data.type, data.details);
    });
  } else {
    console.warn("HLS not supported in this browser");
  }
}

function setPlayState(playing) {
  isPlaying = playing;
  iconPlay.classList.toggle("hidden", playing);
  iconPause.classList.toggle("hidden", !playing);
}

// ---------------------------------------------------------------------------
// WebSocket — live metadata
// ---------------------------------------------------------------------------

function connectWs() {
  if (ws) ws.close();
  clearTimeout(wsReconnectTimer);

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.event === "hls_ready") {
        clearTimeout(window._hlsFallbackTimer);
        // Re-attach if not playing, OR if the player hit an error/never loaded
        // (readyState 0 = HAVE_NOTHING — happens when a 404 was received before
        // the stream was ready and the media element is now stuck in error state).
        if (!isPlaying || player.readyState < 2) attachPlayer();
        return;
      }
      applyMeta(msg);
    } catch {}
  };

  ws.onclose = () => {
    wsReconnectTimer = setTimeout(connectWs, 3000);
  };
}

function applyMeta(m) {
  // Station name
  if (m.station_name) {
    elStationName.textContent = m.station_name;
    document.title = m.station_name + " — Squelch";
  }

  // Track info
  if (m.artist && m.title) {
    elTrackInfo.textContent = `${m.artist} — ${m.title}`;
    elTrackInfo.classList.remove("muted");
  } else if (m.title) {
    elTrackInfo.textContent = m.title;
    elTrackInfo.classList.remove("muted");
  } else if (!m.station_name) {
    elTrackInfo.textContent = "Ready to tune";
    elTrackInfo.classList.add("muted");
  } else {
    elTrackInfo.textContent = "";
  }

  // Cover art
  if (m.has_art && m.art_url) {
    elArt.src = m.art_url + "?t=" + Date.now(); // bust cache
  } else {
    elArt.src = "/static/placeholder.svg";
  }

  // Badges
  toggleEl(elHdBadge, m.hd_locked);
  if (m.pty) {
    elPtyBadge.textContent = m.pty;
    elPtyBadge.classList.remove("hidden");
  } else {
    elPtyBadge.classList.add("hidden");
  }

  // Stereo indicator
  toggleEl(elStereo, m.stereo);

  // Signal bars
  const b = m.signal_bars || 0;
  bars.forEach(bar => bar.classList.toggle("active", Number(bar.dataset.n) <= b));
}

function toggleEl(el, show) {
  el.classList.toggle("hidden", !show);
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
  if (!presets.length) {
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
      <button class="btn-delete" title="Delete">×</button>
    `;
    item.querySelector(".btn-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (confirm(`Delete "${p.name}"?`)) {
        await api("DELETE", `/presets/${p.id}`);
        loadPresets();
      }
    });
    item.addEventListener("click", () => tune(p.frequency, p.band));
    presetsList.appendChild(item);
  });
}

async function saveCurrentPreset(name) {
  await api("POST", "/presets", {
    name,
    frequency: currentFreq,
    band: currentBand,
    gain: selGain.value,
    bandwidth: selBandwidth.value,
    stereo_mode: selStereo.value,
  });
  loadPresets();
}

// ---------------------------------------------------------------------------
// Recordings
// ---------------------------------------------------------------------------

async function loadRecordings() {
  const recs = await api("GET", "/recordings");
  recordingsList.innerHTML = "";
  if (!recs.length) {
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
      <a class="btn-small" href="/recordings/${r.id}/download" download="${esc(r.filename)}">↓</a>
      <button class="btn-delete" title="Delete">×</button>
    `;
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

function formatDuration(s) {
  const m = Math.floor(s / 60), sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function esc(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

// Band tabs
document.querySelectorAll(".band-tab").forEach(tab => {
  tab.addEventListener("click", () => setBand(tab.dataset.band));
});

// Dial scrub
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
  } else if (player.error || player.readyState < 2) {
    // Media element is in error or unloaded state — re-attach cleanly
    attachPlayer();
  } else if (!player.src && !hlsInstance) {
    attachPlayer();
  } else {
    player.play().then(() => setPlayState(true)).catch(() => {});
  }
});

player.addEventListener("play",  () => setPlayState(true));
player.addEventListener("pause", () => setPlayState(false));
player.addEventListener("ended", () => setPlayState(false));
player.addEventListener("error", () => setPlayState(false));

// Volume
volume.addEventListener("input", () => { player.volume = parseFloat(volume.value); });

// Record
btnRecord.addEventListener("click", async () => {
  if (isRecording) {
    await api("POST", "/record/stop");
    isRecording = false;
    btnRecord.classList.remove("active");
    loadRecordings();
  } else {
    const res = await api("POST", "/record/start");
    if (!res.error) {
      isRecording = true;
      btnRecord.classList.add("active");
    }
  }
});

// Save preset
btnSavePreset.addEventListener("click", () => {
  presetNameInput.value = elStationName.textContent !== "Squelch"
    ? elStationName.textContent : "";
  modalPreset.classList.remove("hidden");
  presetNameInput.focus();
});
btnPresetCancel.addEventListener("click", () => modalPreset.classList.add("hidden"));
btnPresetSave.addEventListener("click", async () => {
  const name = presetNameInput.value.trim();
  if (name) {
    await saveCurrentPreset(name);
    modalPreset.classList.add("hidden");
  }
});
presetNameInput.addEventListener("keydown", e => { if (e.key === "Enter") btnPresetSave.click(); });
modalPreset.addEventListener("click", e => { if (e.target === modalPreset) modalPreset.classList.add("hidden"); });

// Quality controls — apply on change when already tuned
selStereo.addEventListener("change",    () => api("POST", "/tune", buildTunePayload()));
selBandwidth.addEventListener("change", () => api("POST", "/tune", buildTunePayload()));
selGain.addEventListener("change",      () => api("POST", "/tune", buildTunePayload()));
inputSquelch.addEventListener("input",  () => { squelchVal.textContent = inputSquelch.value; });

function buildTunePayload() {
  return {
    frequency: currentFreq,
    band: currentBand,
    gain: selGain.value,
    bandwidth: selBandwidth.value,
    stereo_mode: selStereo.value,
  };
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

setBand("fm");
connectWs();
loadPresets();
loadRecordings();

// Check if already recording (page reload)
api("GET", "/record/status").then(s => {
  if (s.recording) {
    isRecording = true;
    btnRecord.classList.add("active");
  }
});
