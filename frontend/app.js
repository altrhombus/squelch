/* Squelch frontend — vanilla JS, no build step.
   Golden Gate redesign: one radio surface, scrub-ruler tuning,
   flush library sidebar, hold-to-seek, glass intensity setting. */

"use strict";

// ─────────────────────────────────────────────────────────────────────────────
// Band model
// ─────────────────────────────────────────────────────────────────────────────

const BANDS = {
  fm:      { min: 87.5, max: 108.0, step: 0.1,   seekStep: 0.2,  unit: "MHz",
             pxPerUnit: 56,  minorTick: 0.5, majorTick: 2,   decimals: 1, ruler: true  },
  hd:      { min: 87.5, max: 108.0, step: 0.1,   seekStep: 0.2,  unit: "MHz",
             pxPerUnit: 56,  minorTick: 0.5, majorTick: 2,   decimals: 1, ruler: true  },
  am:      { min: 530,  max: 1700,  step: 10,    seekStep: 10,   unit: "kHz",
             pxPerUnit: 0.9, minorTick: 20,  majorTick: 200, decimals: 0, ruler: true  },
  wx:      { min: 162.4, max: 162.55, step: 0.025, seekStep: 0.025, unit: "MHz",
             decimals: 3, ruler: false },
  scanner: { min: 25.0, max: 1300.0, step: 0.025, seekStep: 0.025, unit: "MHz",
             decimals: 3, ruler: false },
};

const WX_CHANNELS = [
  { name: "WX1", freq: 162.550 }, { name: "WX2", freq: 162.400 },
  { name: "WX3", freq: 162.475 }, { name: "WX4", freq: 162.425 },
  { name: "WX5", freq: 162.450 }, { name: "WX6", freq: 162.500 },
  { name: "WX7", freq: 162.525 },
];

// Where each band starts when it has never been tuned on this device
const BAND_START = { fm: 91.1, hd: 91.1, am: 1000, wx: 162.4, scanner: 121.5 };

// FM and HD share one dial position: switching to HD means "this station,
// in HD", not a jump to a separately remembered frequency.
function bandMemKey(band) { return band === "hd" ? "fm" : band; }

function savedBandFreq(band) {
  const v = parseFloat(localStorage.getItem(`squelch.freq.${bandMemKey(band)}`));
  return isNaN(v) ? BAND_START[band] : clamp(v, BANDS[band].min, BANDS[band].max);
}

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────

let currentBand  = "fm";
let displayFreq  = 91.1;      // what the ruler/readout show (live during scrub)
let isPlaying    = false;
let isRecording  = false;
let _seekActive  = false;     // a server-side seek scan is running
let _seekStartedAt = 0;       // grace window so an in-flight stale frame can't cancel it
let ws = null, wsReconnectTimer = null;

let _prevHasArt = false, _prevArtUrl = "", _prevArtVersion = -1, _prevHdLocked = false;
let _currentAppleMusicUrl = null;
let _prevTrackKey = "", _historyRefreshTimer = null;
let _lastMeta = null, _mediaSessionReady = false;
let _recTimer = null, _recStart = null;
let _lastTrackChangeAt = Date.now();
let _autoHdFreq = null;   // frequency already auto-switched to HD this visit
let _presetMarks = [];        // frequencies of saved presets, drawn on the ruler
let _scanEntry = null;        // keypad entry string while typing (scanner)

const $ = (id) => document.getElementById(id);

const player      = $("player");
const rulerCanvas = $("ruler");
const readoutVal  = $("freq-value");
const readoutUnit = $("freq-unit");

// iOS ignores programmatic media-element volume — hide the slider there.
const IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
if (IS_IOS) document.body.classList.add("ios");

// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────

function clamp(v, lo, hi) { return Math.min(Math.max(Number(v), lo), hi); }

function snap(freq, band = currentBand) {
  const b = BANDS[band];
  const snapped = Math.round((freq - b.min) / b.step) * b.step + b.min;
  return clamp(parseFloat(snapped.toFixed(4)), b.min, b.max);
}

function formatFreq(f, band = currentBand) {
  return Number(f).toFixed(BANDS[band].decimals);
}

function esc(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

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

function showToast(msg) {
  const container = $("toast-container");
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.classList.add("toast-out"); setTimeout(() => el.remove(), 220); }, 2200);
}

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - Number(ts);
  if (diff < 60)    return "just now";
  if (diff < 3600)  return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function formatDuration(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Tuning — display vs commit
// ─────────────────────────────────────────────────────────────────────────────

let _commitTimer = null;
let _pendingCommit = false;   // a local tune is in flight — don't let WS state fight it

function setDisplayFreq(f, { commit = true } = {}) {
  displayFreq = clamp(f, BANDS[currentBand].min, BANDS[currentBand].max);
  readoutVal.textContent = formatFreq(displayFreq);
  readoutUnit.textContent = BANDS[currentBand].unit;
  rulerCanvas.setAttribute("aria-valuenow", displayFreq);
  drawRuler();
  updateWxActive();
  if (commit) {
    // Debounced: scrubs, arrow keys, and step taps coalesce into one tune.
    _pendingCommit = true;
    clearTimeout(_commitTimer);
    _commitTimer = setTimeout(() => commitTune(), 300);
  }
}

async function commitTune(band = currentBand, extra = {}) {
  cancelSeek();
  _pendingCommit = false;
  const body = {
    frequency: snap(displayFreq, band),
    band,
    gain: "auto",
    stereo_mode: "auto",
    ...extra,
  };
  localStorage.setItem(`squelch.freq.${bandMemKey(band)}`, String(body.frequency));
  // Start the stream inside a user-gesture call stack when not already
  // playing (Safari autoplay policy); when playing, the <audio> element is
  // left alone — the backend retunes without dropping the connection.
  if (!isPlaying) _startStream();
  const res = await api("POST", "/tune", body);
  if (!res.error) applySquelchForBand(band);
  return res;
}

function tune(freq, band, extra = {}) {
  if (band && band !== currentBand) setBand(band, { retune: false });
  clearTimeout(_commitTimer);
  setDisplayFreq(freq, { commit: false });
  return commitTune(band || currentBand, extra);
}

// ─────────────────────────────────────────────────────────────────────────────
// Band switching
// ─────────────────────────────────────────────────────────────────────────────

function setBand(band, { retune = true } = {}) {
  cancelSeek();
  _scanEntry = null;
  currentBand = band;
  const b = BANDS[band];

  document.querySelectorAll("#band-tabs .seg-btn").forEach(t => {
    const active = t.dataset.band === band;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
  });

  $("ruler-row").classList.toggle("hidden", !b.ruler);
  $("wx-panel").classList.toggle("hidden", band !== "wx");
  $("scan-panel").classList.toggle("hidden", band !== "scanner");
  $("squelch-row").classList.toggle("hidden", band !== "wx" && band !== "scanner");
  if (band !== "hd") { $("hd-channels").innerHTML = ""; $("hd-channels").classList.add("hidden"); }

  rulerCanvas.setAttribute("aria-valuemin", b.min);
  rulerCanvas.setAttribute("aria-valuemax", b.max);

  loadSquelchUi(band);
  localStorage.setItem("squelch.band", band);
  // Per-band memory: each band remembers its last tuned frequency on this
  // device (FM → WX → back to FM used to clamp 162.4 into 108.0).
  setDisplayFreq(savedBandFreq(band), { commit: false });
  if (retune) commitTune(band);
}

// ─────────────────────────────────────────────────────────────────────────────
// Ruler — a tape that moves under a fixed needle
// ─────────────────────────────────────────────────────────────────────────────

function drawRuler() {
  const b = BANDS[currentBand];
  if (!b.ruler || rulerCanvas.offsetWidth === 0) return;

  const dpr = window.devicePixelRatio || 1;
  const W = rulerCanvas.offsetWidth, H = rulerCanvas.offsetHeight;
  rulerCanvas.width = W * dpr;
  rulerCanvas.height = H * dpr;
  const ctx = rulerCanvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const cs = getComputedStyle(document.documentElement);
  const ink = cs.getPropertyValue("--ink").trim() || "#f2f2f4";
  const accent = cs.getPropertyValue("--accent-dynamic").trim() || "#e05c00";

  const cx = W / 2;
  const fToX = (f) => cx + (f - displayFreq) * b.pxPerUnit;

  const fLo = displayFreq - cx / b.pxPerUnit;
  const fHi = displayFreq + cx / b.pxPerUnit;

  // Minor ticks
  ctx.strokeStyle = ink; ctx.globalAlpha = 0.18; ctx.lineWidth = 1;
  ctx.beginPath();
  for (let f = Math.ceil(Math.max(fLo, b.min) / b.minorTick) * b.minorTick;
       f <= Math.min(fHi, b.max) + 1e-9; f += b.minorTick) {
    const x = fToX(f);
    ctx.moveTo(x, H * 0.62); ctx.lineTo(x, H - 10);
  }
  ctx.stroke();

  // Major ticks + labels
  ctx.globalAlpha = 0.4;
  ctx.beginPath();
  const majors = [];
  for (let f = Math.ceil(Math.max(fLo, b.min) / b.majorTick) * b.majorTick;
       f <= Math.min(fHi, b.max) + 1e-9; f += b.majorTick) {
    const x = fToX(f);
    ctx.moveTo(x, H * 0.46); ctx.lineTo(x, H - 10);
    majors.push([f, x]);
  }
  ctx.stroke();
  ctx.globalAlpha = 0.55;
  ctx.fillStyle = ink;
  ctx.font = `600 11px ${cs.getPropertyValue("--font") || "-apple-system, sans-serif"}`;
  ctx.textAlign = "center";
  for (const [f, x] of majors) ctx.fillText(String(Math.round(f)), x, H * 0.34);

  // Band edges
  ctx.globalAlpha = 0.3; ctx.strokeStyle = ink; ctx.lineWidth = 2;
  for (const edge of [b.min, b.max]) {
    const x = fToX(edge);
    if (x > -2 && x < W + 2) {
      ctx.beginPath(); ctx.moveTo(x, H * 0.3); ctx.lineTo(x, H - 8); ctx.stroke();
    }
  }

  // Preset markers — your stations, living on the dial
  ctx.globalAlpha = 0.95; ctx.fillStyle = accent;
  for (const pf of _presetMarks) {
    const x = fToX(pf);
    if (x > 4 && x < W - 4) {
      ctx.beginPath(); ctx.arc(x, H * 0.2, 2.6, 0, Math.PI * 2); ctx.fill();
    }
  }

  // Needle — fixed centre, art-tinted glow
  ctx.globalAlpha = 1;
  ctx.save();
  ctx.strokeStyle = accent; ctx.lineWidth = 2;
  ctx.shadowBlur = 10; ctx.shadowColor = accent;
  ctx.beginPath(); ctx.moveTo(cx, 8); ctx.lineTo(cx, H - 8); ctx.stroke();
  ctx.restore();
  ctx.fillStyle = accent;
  ctx.beginPath();
  ctx.moveTo(cx - 5, 0); ctx.lineTo(cx + 5, 0); ctx.lineTo(cx, 9); ctx.closePath();
  ctx.fill();
}

let _scrubbing = false;   // finger on the ruler or momentum running

function setupRuler() {
  let dragging = false, lastX = 0, lastT = 0, velocity = 0, moved = 0;
  let momentumRaf = null;

  const stopMomentum = () => {
    if (momentumRaf) cancelAnimationFrame(momentumRaf);
    momentumRaf = null;
    _scrubbing = false;
  };

  rulerCanvas.addEventListener("pointerdown", (e) => {
    if (!BANDS[currentBand].ruler) return;
    cancelSeek();
    stopMomentum();
    dragging = true; _scrubbing = true; moved = 0;
    lastX = e.clientX; lastT = performance.now(); velocity = 0;
    rulerCanvas.setPointerCapture(e.pointerId);
  });

  rulerCanvas.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const now = performance.now();
    const dx = e.clientX - lastX;
    moved += Math.abs(dx);
    const dt = Math.max(1, now - lastT);
    velocity = 0.7 * velocity + 0.3 * (dx / dt);   // px/ms, smoothed
    lastX = e.clientX; lastT = now;
    setDisplayFreq(displayFreq - dx / BANDS[currentBand].pxPerUnit, { commit: false });
  });

  const release = (e) => {
    if (!dragging) return;
    dragging = false;
    if (moved < 5) {
      // Tap: jump to the frequency under the finger
      _scrubbing = false;
      const rect = rulerCanvas.getBoundingClientRect();
      const f = displayFreq + (e.clientX - rect.left - rect.width / 2) / BANDS[currentBand].pxPerUnit;
      animateTo(snap(f));
      return;
    }
    // Momentum, then snap + commit
    const decay = () => {
      velocity *= 0.93;
      if (Math.abs(velocity) < 0.02) {
        stopMomentum();
        setDisplayFreq(snap(displayFreq));   // snap + debounced commit
        return;
      }
      setDisplayFreq(displayFreq - velocity * 16 / BANDS[currentBand].pxPerUnit, { commit: false });
      momentumRaf = requestAnimationFrame(decay);
    };
    if (Math.abs(velocity) > 0.05) momentumRaf = requestAnimationFrame(decay);
    else { _scrubbing = false; setDisplayFreq(snap(displayFreq)); }
  };
  rulerCanvas.addEventListener("pointerup", release);
  rulerCanvas.addEventListener("pointercancel", () => { dragging = false; _scrubbing = false; });

  // Trackpad / wheel
  rulerCanvas.addEventListener("wheel", (e) => {
    if (!BANDS[currentBand].ruler) return;
    e.preventDefault();
    const d = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY);
    setDisplayFreq(displayFreq + d / BANDS[currentBand].pxPerUnit, { commit: false });
    clearTimeout(_commitTimer);
    _commitTimer = setTimeout(() => setDisplayFreq(snap(displayFreq)), 220);
  }, { passive: false });

  // Keyboard
  rulerCanvas.addEventListener("keydown", (e) => {
    const b = BANDS[currentBand];
    if (e.key === "ArrowLeft")  { e.preventDefault(); setDisplayFreq(snap(displayFreq - b.step)); }
    if (e.key === "ArrowRight") { e.preventDefault(); setDisplayFreq(snap(displayFreq + b.step)); }
  });

  new ResizeObserver(drawRuler).observe(rulerCanvas);
}

function animateTo(target) {
  const start = displayFreq, dist = target - start, t0 = performance.now();
  const dur = 320;
  const ease = (t) => 1 - Math.pow(1 - t, 3);
  const step = (now) => {
    const t = Math.min(1, (now - t0) / dur);
    setDisplayFreq(start + dist * ease(t), { commit: false });
    if (t < 1) requestAnimationFrame(step);
    else setDisplayFreq(target);     // final: snap + debounced commit
  };
  requestAnimationFrame(step);
}

// ─────────────────────────────────────────────────────────────────────────────
// Step buttons: tap = one step, hold = seek scan
// ─────────────────────────────────────────────────────────────────────────────

function setupStepButton(btn, dir) {
  let holdTimer = null, held = false;

  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    held = false;
    holdTimer = setTimeout(() => { held = true; startSeek(dir, btn); }, 420);
  });
  const finish = () => {
    clearTimeout(holdTimer);
    // A short press steps one channel; a long press already started a
    // server-side seek (which keeps running after release, car-radio
    // style — tap anything to stop).
    if (!held && !_seekActive) {
      const b = BANDS[currentBand];
      setDisplayFreq(snap(displayFreq + dir * b.step));
    }
  };
  btn.addEventListener("pointerup", finish);
  btn.addEventListener("pointerleave", () => clearTimeout(holdTimer));
  btn.addEventListener("pointercancel", () => clearTimeout(holdTimer));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const b = BANDS[currentBand];
      setDisplayFreq(snap(displayFreq + dir * b.step));
    }
  });
}

// Seeking is done server-side: the pipeline sweeps the band between block
// reads (measuring each channel's pilot/noise directly, no polling race)
// and stops on the next listenable station.  Clients just start/stop it
// and follow the needle via the /ws frequency + `seeking` updates.
async function startSeek(dir, btn) {
  if (currentBand !== "fm") return;   // FM only for now
  if (_seekActive) { cancelSeek(); return; }
  _seekActive = true;
  _seekStartedAt = Date.now();
  if (!isPlaying) _startStream();
  document.querySelectorAll(".btn-chev").forEach(b => b.classList.remove("seeking"));
  btn.classList.add("seeking");
  $("seek-state").classList.remove("hidden");
  const res = await api("POST", "/seek", { direction: dir > 0 ? "up" : "down" });
  if (!res.seeking) cancelSeek();
}

function cancelSeek() {
  if (!_seekActive) return;
  _seekActive = false;
  document.querySelectorAll(".btn-chev").forEach(b => b.classList.remove("seeking"));
  $("seek-state").classList.add("hidden");
  api("POST", "/seek/stop");
}

// ─────────────────────────────────────────────────────────────────────────────
// WX chips + scanner keypad + squelch
// ─────────────────────────────────────────────────────────────────────────────

function buildWxPanel() {
  const panel = $("wx-panel");
  panel.innerHTML = "";
  for (const { name, freq } of WX_CHANNELS) {
    const btn = document.createElement("button");
    btn.className = "wx-chip";
    btn.dataset.freq = freq;
    btn.innerHTML = `<span class="wx-name">${name}</span><span class="wx-freq">${freq.toFixed(3)}</span>`;
    btn.addEventListener("click", () => tune(freq, "wx"));
    panel.appendChild(btn);
  }
}

function updateWxActive() {
  if (currentBand !== "wx") return;
  document.querySelectorAll(".wx-chip").forEach(c => {
    const match = Math.abs(parseFloat(c.dataset.freq) - displayFreq) < 0.001;
    c.classList.toggle("active", match);
    c.setAttribute("aria-pressed", String(match));
  });
}

function buildKeypad() {
  const pad = $("keypad");
  pad.innerHTML = "";
  const keys = ["1","2","3","4","5","6","7","8","9",".","0","⌫"];
  for (const k of keys) {
    const btn = document.createElement("button");
    btn.className = "key";
    btn.textContent = k;
    btn.setAttribute("aria-label", k === "⌫" ? "Delete" : k);
    btn.addEventListener("click", () => keypadPress(k));
    pad.appendChild(btn);
  }
  const go = document.createElement("button");
  go.className = "key key-go";
  go.textContent = "Tune";
  go.addEventListener("click", keypadCommit);
  pad.appendChild(go);
}

function keypadPress(k) {
  if (_scanEntry === null) _scanEntry = "";
  if (k === "⌫") _scanEntry = _scanEntry.slice(0, -1);
  else if (k === "." && _scanEntry.includes(".")) return;
  else if (_scanEntry.length < 8) _scanEntry += k;
  readoutVal.textContent = _scanEntry || "—";
}

function keypadCommit() {
  const v = parseFloat(_scanEntry);
  _scanEntry = null;
  if (isNaN(v)) { readoutVal.textContent = formatFreq(displayFreq); return; }
  tune(clamp(v, BANDS.scanner.min, BANDS.scanner.max), "scanner");
}

let _squelchTimer = null;

function setupSquelch() {
  const slider = $("squelch");
  slider.addEventListener("input", () => {
    const v = parseInt(slider.value, 10);
    $("squelch-val").textContent = v === 0 ? "Off" : `${v}%`;
    localStorage.setItem(`squelch.sq.${currentBand}`, String(v));
    clearTimeout(_squelchTimer);
    _squelchTimer = setTimeout(() => api("POST", "/squelch", { slider: v }), 250);
  });
}

function loadSquelchUi(band) {
  const v = parseInt(localStorage.getItem(`squelch.sq.${band}`) || "0", 10);
  $("squelch").value = v;
  $("squelch-val").textContent = v === 0 ? "Off" : `${v}%`;
}

function applySquelchForBand(band) {
  const v = (band === "wx" || band === "scanner")
    ? parseInt(localStorage.getItem(`squelch.sq.${band}`) || "0", 10)
    : 0;
  api("POST", "/squelch", { slider: v });
}

// ─────────────────────────────────────────────────────────────────────────────
// Stream + transport
// ─────────────────────────────────────────────────────────────────────────────

function _startStream() {
  player.src = "/stream?" + Date.now();
  player.muted = true;
  player.play()
    .then(() => {
      player.muted = false;
      setPlayState(true);
      setupMediaSession();
    })
    .catch(err => {
      player.muted = false;
      if (err?.name === "NotAllowedError") {
        $("track-title").textContent = "Tap ▶ to start";
        $("track-title").classList.add("muted");
      }
    });
}

function setPlayState(playing) {
  isPlaying = playing;
  $("icon-play").classList.toggle("hidden", playing);
  $("icon-stop").classList.toggle("hidden", !playing);
  $("btn-play").setAttribute("aria-pressed", String(playing));
  $("btn-play").setAttribute("aria-label", playing ? "Stop" : "Play");
  updateMediaSession(null);
}

// ─────────────────────────────────────────────────────────────────────────────
// Metadata (WebSocket) → UI
// ─────────────────────────────────────────────────────────────────────────────

function connectWs() {
  if (ws) ws.close();
  clearTimeout(wsReconnectTimer);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    $("ws-status").classList.add("hidden");
    syncRecordingState();
  };
  ws.onmessage = (e) => { try { applyMeta(JSON.parse(e.data)); } catch {} };
  ws.onclose = () => {
    $("ws-status").classList.remove("hidden");
    wsReconnectTimer = setTimeout(connectWs, 3000);
  };
}

function applyMeta(m) {
  if (m.event) return;   // event-only frames
  _lastMeta = m;

  // Keep the seek UI in sync with the server's authoritative flag: a seek
  // that finds a station (or exhausts the band) clears it here; a seek
  // started from another device lights it up here.  A brief grace window
  // ignores a stale in-flight frame that predates our own start request.
  const seekGrace = Date.now() - _seekStartedAt < 1500;
  if (m.seeking && !_seekActive) {
    _seekActive = true;
    $("seek-state").classList.remove("hidden");
  } else if (!m.seeking && _seekActive && !seekGrace) {
    _seekActive = false;
    $("seek-state").classList.add("hidden");
    document.querySelectorAll(".btn-chev").forEach(b => b.classList.remove("seeking"));
  }

  // The dial mirrors the ACTUAL radio — this covers loading the page
  // while another device is listening, tunes made elsewhere, and the
  // needle sweeping during a server-side seek.  It stands down only while
  // THIS user is mid-interaction (scrubbing, an uncommitted debounce, or
  // keypad entry).
  if (m.frequency && m.band && BANDS[m.band]
      && !_scrubbing && !_pendingCommit && _scanEntry === null) {
    const f = m.band === "am" ? m.frequency / 1e3 : m.frequency / 1e6;
    if (m.band !== currentBand) {
      setBand(m.band, { retune: false });
    }
    if (Math.abs(f - displayFreq) > BANDS[m.band].step / 2) {
      setDisplayFreq(clamp(f, BANDS[m.band].min, BANDS[m.band].max), { commit: false });
    }
  }

  // Station name + title
  if (m.station_name) {
    $("station-name").textContent = m.station_name;
    document.title = m.station_name + " — Squelch";
  } else if (m.frequency && m.band && m.state !== "idle") {
    const unit = m.band === "am" ? "kHz" : "MHz";
    const freq = m.band === "am"
      ? Math.round(m.frequency / 1e3)
      : (m.frequency / 1e6).toFixed(BANDS[m.band]?.decimals ?? 1);
    $("station-name").textContent = `${freq} ${unit}`;
    document.title = `${freq} ${unit} — Squelch`;
  } else {
    $("station-name").textContent = "Squelch";
    document.title = "Squelch";
  }

  // Slogan
  const slogan = $("station-slogan");
  slogan.textContent = m.slogan || "";
  slogan.classList.toggle("hidden", !m.slogan);

  // Track info
  const trackKey = `${m.artist || ""}|${m.title || ""}`;
  const elTitle = $("track-title"), elArtist = $("track-artist");
  if (m.title) {
    elTitle.textContent = m.title;
    elTitle.classList.remove("muted");
    elArtist.textContent = m.artist || "";
    elArtist.classList.toggle("hidden", !m.artist);
  } else {
    const waitedLong = Date.now() - _lastTrackChangeAt > 12_000;
    const hint = {
      idle:      "Ready to tune",
      tuning:    "Tuning…",
      buffering: "Buffering…",
      live:      (m.band === "fm" || m.band === "hd")
                   ? (waitedLong ? "On air" : "Waiting for station info…")
                   : "On air",
      error:     "Radio error — try retuning",
    }[m.state] || "";
    elTitle.textContent = hint;
    elTitle.classList.add("muted");
    elArtist.classList.add("hidden");
  }
  if (trackKey !== _prevTrackKey) {
    _prevTrackKey = trackKey;
    _lastTrackChangeAt = Date.now();
    if (m.artist || m.title) {
      const info = $("now-info");
      info.classList.remove("track-changing");
      void info.offsetWidth;
      info.classList.add("track-changing");
      info.addEventListener("animationend", () => info.classList.remove("track-changing"), { once: true });
      clearTimeout(_historyRefreshTimer);
      _historyRefreshTimer = setTimeout(loadHistory, 6000);
    }
  }

  // Art
  const nowHasArt = !!(m.has_art && m.art_url);
  const artVersion = m.art_version ?? -1;
  if (nowHasArt !== _prevHasArt || (nowHasArt && (m.art_url !== _prevArtUrl || artVersion !== _prevArtVersion))) {
    _prevHasArt = nowHasArt; _prevArtUrl = m.art_url || ""; _prevArtVersion = artVersion;
    updateArt(nowHasArt ? m.art_url : "/static/placeholder.svg");
  }
  _currentAppleMusicUrl = m.apple_music_url || null;
  updateArtLink(m);

  // Badges
  const pty = $("pty-badge");
  pty.textContent = m.pty || "";
  pty.classList.toggle("hidden", !m.pty);
  $("stereo-badge").classList.toggle("hidden", !m.stereo);

  // HD badge — the car-radio convention: HOLLOW badge = HD is available
  // on this station (tap to switch), SOLID accent badge = you are
  // actually listening to HD.  Zero ambiguity about which one you're in.
  const hd = $("hd-badge");
  const hdAvailable = !!m.hd_available && currentBand === "fm" && !m.hd_locked;
  hd.classList.toggle("hd-available", hdAvailable);
  hd.classList.toggle("hd-locked", !!m.hd_locked);
  hd.classList.toggle("hidden", !m.hd_locked && !hdAvailable);
  if (hdAvailable) {
    hd.textContent = "HD available";
    hd.setAttribute("title", "This station broadcasts HD — tap to switch");
    hd.setAttribute("aria-label", "HD available on this station, tap to switch to HD");
  } else {
    hd.textContent = "HD";
    hd.removeAttribute("title");
    hd.setAttribute("aria-label", m.hd_locked ? "Listening in HD" : "HD Radio");
  }
  if (m.hd_locked && !_prevHdLocked) showToast("HD Radio locked");
  _prevHdLocked = !!m.hd_locked;

  // Auto-HD (opt-in): when the station broadcasts HD, switch to it —
  // once per frequency, so switching back to FM by hand is respected.
  if (hdAvailable && localStorage.getItem("squelch.autohd") === "1"
      && !_scrubbing && !_pendingCommit && !_seekActive && !m.seeking
      && _autoHdFreq !== snap(displayFreq)) {
    _autoHdFreq = snap(displayFreq);
    showToast("HD detected — switching");
    tune(displayFreq, "hd");
  }

  // Signal bars
  const bars = m.signal_bars || 0;
  const labels = ["No signal", "Poor", "Weak", "Fair", "Good", "Excellent"];
  $("signal-meter").setAttribute("aria-label", `Signal: ${labels[bars]}`);
  document.querySelectorAll("#signal-meter .bar")
    .forEach(bar => bar.classList.toggle("active", Number(bar.dataset.n) <= bars));

  // HD sub-channels
  const hdCh = $("hd-channels");
  if (m.hd_locked && Array.isArray(m.hd_channels_available) && m.hd_channels_available.length > 1) {
    hdCh.innerHTML = m.hd_channels_available.map(ch => {
      const active = ch === (m.hd_channel || 1);
      return `<button class="chip${active ? " active" : ""}" data-ch="${ch}" aria-pressed="${active}">HD${ch}</button>`;
    }).join("");
    hdCh.querySelectorAll(".chip").forEach(btn =>
      btn.addEventListener("click", () => tune(displayFreq, currentBand, { hd_channel: +btn.dataset.ch })));
    hdCh.classList.remove("hidden");
  } else {
    hdCh.classList.add("hidden");
  }

  if (m.diag) renderDiag(m.diag, m.band);
  updateMediaSession(m);
}

$("hd-badge").addEventListener("click", () => {
  if ($("hd-badge").classList.contains("hd-available")) tune(displayFreq, "hd");
});

// ─────────────────────────────────────────────────────────────────────────────
// Art crossfade + dominant color
// ─────────────────────────────────────────────────────────────────────────────

function updateArt(rawUrl) {
  const hasNewArt = rawUrl !== "/static/placeholder.svg";
  const src = hasNewArt ? rawUrl + "?t=" + Date.now() : "/static/placeholder.svg";
  const elArt = $("art");

  $("art-back").src = elArt.src || "/static/placeholder.svg";
  elArt.classList.add("art-loading");
  $("art-blur-bg").style.backgroundImage = hasNewArt ? `url("${src}")` : "none";

  const img = new Image();
  img.onload = () => {
    elArt.src = img.src;
    requestAnimationFrame(() => elArt.classList.remove("art-loading"));
    const root = document.documentElement;
    const color = hasNewArt ? extractDominantColor(img) : null;
    if (color) {
      root.style.setProperty("--accent-dynamic", color);
      root.style.setProperty("--accent-glow", color.replace("rgb", "rgba").replace(")", ", 0.25)"));
      const m = color.match(/rgb\((\d+),(\d+),(\d+)\)/);
      if (m) {
        root.style.setProperty("--art-r", m[1]);
        root.style.setProperty("--art-g", m[2]);
        root.style.setProperty("--art-b", m[3]);
      }
    } else {
      root.style.setProperty("--accent-dynamic", "var(--accent)");
      root.style.setProperty("--accent-glow", "rgba(224, 92, 0, 0.25)");
      root.style.setProperty("--art-r", "224");
      root.style.setProperty("--art-g", "92");
      root.style.setProperty("--art-b", "0");
    }
    drawRuler();
  };
  img.onerror = () => {
    elArt.src = "/static/placeholder.svg";
    elArt.classList.remove("art-loading");
    $("art-blur-bg").style.backgroundImage = "none";
  };
  img.src = src;
}

function extractDominantColor(imgEl) {
  try {
    const canvas = $("color-canvas");
    const ctx = canvas.getContext("2d");
    ctx.drawImage(imgEl, 0, 0, 8, 8);
    const data = ctx.getImageData(0, 0, 8, 8).data;
    let r = 0, g = 0, b = 0, n = 0;
    for (let i = 0; i < data.length; i += 4) {
      const lum = data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
      if (lum > 20 && lum < 220) { r += data[i]; g += data[i + 1]; b += data[i + 2]; n++; }
    }
    if (!n) return null;
    r = Math.round(r / n); g = Math.round(g / n); b = Math.round(b / n);

    const mx = Math.max(r, g, b), k = 1.5;
    r = Math.min(255, Math.round(r * (r === mx ? k : 1 / k)));
    g = Math.min(255, Math.round(g * (g === mx ? k : 1 / k)));
    b = Math.min(255, Math.round(b * (b === mx ? k : 1 / k)));

    const toLinear = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    const lum = 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
    const isLight = window.matchMedia("(prefers-color-scheme: light)").matches;
    if (isLight && lum > 0.35) {
      const s = 0.45 / lum;
      r = Math.round(r * s); g = Math.round(g * s); b = Math.round(b * s);
    } else if (!isLight && lum < 0.08) {
      const s = 0.18 / Math.max(lum, 0.001);
      r = Math.min(255, Math.round(r * s));
      g = Math.min(255, Math.round(g * s));
      b = Math.min(255, Math.round(b * s));
    }
    return `rgb(${r},${g},${b})`;
  } catch { return null; }
}

function updateArtLink(m) {
  const wrap = $("art-wrap");
  let url = _currentAppleMusicUrl;
  if (!url && m.artist && m.title) {
    url = `https://music.apple.com/search?term=${encodeURIComponent(m.artist + " " + m.title)}`;
  }
  if (url) {
    wrap.classList.add("has-link");
    wrap.setAttribute("role", "link");
    wrap.setAttribute("aria-label", "Open in Apple Music");
    wrap.setAttribute("tabindex", "0");
    wrap.onclick = () => window.open(url, "_blank", "noopener");
  } else {
    wrap.classList.remove("has-link");
    wrap.removeAttribute("role"); wrap.removeAttribute("aria-label"); wrap.removeAttribute("tabindex");
    wrap.onclick = null;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Diagnostics popover
// ─────────────────────────────────────────────────────────────────────────────

const DIAG_ROWS = [
  ["iq_rms",          "RF level",   0.7,  v => v > 0.45 ? "weak" : v > 0.1 ? "good" : "fair", v => v.toFixed(3)],
  ["composite_rms",   "Composite",  0.6,  v => v > 0.2 ? "good" : v > 0.05 ? "fair" : "weak", v => v.toFixed(3)],
  ["pilot_rms",       "Pilot",      0.10, v => v > 0.06 ? "good" : v > 0.02 ? "fair" : "weak", v => v.toFixed(4)],
  ["noise_rms",       "Noise",      0.10, v => v < 0.02 ? "good" : v < 0.05 ? "fair" : "weak", v => v.toFixed(4)],
  ["blend",           "Stereo blend", 1.0, v => v > 0.6 ? "good" : v > 0.2 ? "fair" : "weak", v => Math.round(v * 100) + "%"],
  ["audio_rms",       "Audio",      0.5,  v => v > 0.05 && v < 0.45 ? "good" : v >= 0.45 ? "weak" : "fair", v => v.toFixed(3)],
  ["pilot_offset_hz", "Pilot offset", 5,  v => Math.abs(v) < 1 ? "good" : Math.abs(v) < 3 ? "fair" : "weak", v => v.toFixed(2) + " Hz"],
  ["carrier_offset_hz","Carrier off.", 20000, v => Math.abs(v) < 1000 ? "good" : "weak", v => Math.round(v) + " Hz"],
  ["hd_ratio",        "HD ratio",   10,   v => v > 2.5 ? "good" : "fair", v => v.toFixed(2)],
  ["gain_db",         "SDR gain",   50,   v => v <= 35 ? "good" : v <= 42 ? "fair" : "weak", v => v.toFixed(1) + " dB"],
];

let _lastDiag = {};
let _diagRows = null;   // key → { row, fill, num } — built once, updated in place

function _buildDiagRows() {
  const body = $("diag-body");
  body.innerHTML = "";
  _diagRows = {};
  for (const [key, label] of DIAG_ROWS) {
    const row = document.createElement("div");
    row.className = "diag-row hidden";
    row.innerHTML = `<span class="diag-label">${label}</span>
      <span class="diag-track"><span class="diag-fill"></span></span>
      <span class="diag-num">—</span>`;
    body.appendChild(row);
    _diagRows[key] = {
      row, fill: row.querySelector(".diag-fill"), num: row.querySelector(".diag-num"),
    };
  }
}

function renderDiag(d, band) {
  _lastDiag = { ...d, band };
  if ($("diag-popover").classList.contains("hidden")) return;
  if (!_diagRows) _buildDiagRows();
  // Update in place so the CSS width/color transitions actually animate —
  // rebuilding the DOM every WebSocket frame froze them as static lines.
  for (const [key, , maxVal, colorFn, fmt] of DIAG_ROWS) {
    const r = _diagRows[key];
    if (d[key] == null) { r.row.classList.add("hidden"); continue; }
    const v = d[key];
    r.row.classList.remove("hidden");
    r.fill.style.width = Math.min(100, Math.abs(v) / maxVal * 100) + "%";
    r.fill.className = "diag-fill " + colorFn(v);
    r.num.textContent = fmt(v);
  }
}

$("btn-copy-diag").addEventListener("click", () => {
  const info = {
    timestamp: new Date().toISOString(),
    frequency: displayFreq, band: currentBand,
    ...(_lastDiag || {}),
  };
  const text = JSON.stringify(info, null, 2);
  const btn = $("btn-copy-diag");
  const orig = btn.textContent;
  const done = () => { btn.textContent = "Copied!"; setTimeout(() => btn.textContent = orig, 1500); };
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(done).catch(() => {});
});

// ─────────────────────────────────────────────────────────────────────────────
// Popover management
// ─────────────────────────────────────────────────────────────────────────────

let _openPopover = null;

function openPopover(pop, anchor) {
  closePopover();
  pop.classList.remove("hidden");
  const a = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let left = Math.min(a.right - w, window.innerWidth - w - 10);
  left = Math.max(10, left);
  let top = a.bottom + 8;
  if (top + h > window.innerHeight - 10) top = Math.max(10, a.top - h - 8);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  _openPopover = pop;
  if (pop.id === "diag-popover" && _lastMeta?.diag) renderDiag(_lastMeta.diag, _lastMeta.band);
  setTimeout(() => document.addEventListener("pointerdown", _outsideClose), 0);
}

function closePopover() {
  if (!_openPopover) return;
  _openPopover.classList.add("hidden");
  _openPopover = null;
  document.removeEventListener("pointerdown", _outsideClose);
}

function _outsideClose(e) {
  if (_openPopover && !_openPopover.contains(e.target)) closePopover();
}

document.querySelectorAll(".popover-close").forEach(b =>
  b.addEventListener("click", closePopover));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopover(); });

$("signal-cluster").addEventListener("click", (e) => {
  e.stopPropagation();
  openPopover($("diag-popover"), $("signal-cluster"));
});
for (const id of ["btn-settings", "btn-settings-phone"]) {
  $(id)?.addEventListener("click", (e) => {
    e.stopPropagation();
    openPopover($("settings-popover"), e.currentTarget);
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Glass intensity — the Golden Gate slider, persisted
// ─────────────────────────────────────────────────────────────────────────────

function setGlass(level, save = true) {
  document.documentElement.dataset.glass = level;
  document.querySelectorAll("#glass-seg .seg-btn").forEach(b => {
    const on = b.dataset.glass === level;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", String(on));
  });
  if (save) localStorage.setItem("squelch.glass", level);
}

document.querySelectorAll("#glass-seg .seg-btn").forEach(b =>
  b.addEventListener("click", () => setGlass(b.dataset.glass)));

function setAutoHd(on, save = true) {
  document.querySelectorAll("#autohd-seg .seg-btn").forEach(b => {
    const active = b.dataset.autohd === (on ? "1" : "0");
    b.classList.toggle("active", active);
    b.setAttribute("aria-checked", String(active));
  });
  if (save) localStorage.setItem("squelch.autohd", on ? "1" : "0");
  if (!on) _autoHdFreq = null;
}

document.querySelectorAll("#autohd-seg .seg-btn").forEach(b =>
  b.addEventListener("click", () => setAutoHd(b.dataset.autohd === "1")));

// ─────────────────────────────────────────────────────────────────────────────
// Media Session
// ─────────────────────────────────────────────────────────────────────────────

function setupMediaSession() {
  if (!("mediaSession" in navigator) || _mediaSessionReady) return;
  _mediaSessionReady = true;
  navigator.mediaSession.setActionHandler("play", () => _startStream());
  navigator.mediaSession.setActionHandler("pause", () => { player.pause(); setPlayState(false); });
  navigator.mediaSession.setActionHandler("stop",  () => { player.pause(); setPlayState(false); });
  navigator.mediaSession.setActionHandler("previoustrack", () =>
    setDisplayFreq(snap(displayFreq - BANDS[currentBand].step)));
  navigator.mediaSession.setActionHandler("nexttrack", () =>
    setDisplayFreq(snap(displayFreq + BANDS[currentBand].step)));
  if (_lastMeta) updateMediaSession(_lastMeta);
}

function updateMediaSession(m) {
  if (!("mediaSession" in navigator)) return;
  if (m !== null) {
    const artSrc = (m?.has_art && m?.art_url)
      ? location.origin + m.art_url + "?t=" + (m.art_version || 0)
      : location.origin + "/static/placeholder.svg";
    navigator.mediaSession.metadata = new MediaMetadata({
      title:  m?.title || `${formatFreq(displayFreq)} ${BANDS[currentBand].unit}`,
      artist: m?.artist || m?.station_name || "",
      album:  m?.station_name || m?.slogan || "Squelch",
      artwork: [
        { src: artSrc, sizes: "512x512", type: "image/jpeg" },
        { src: artSrc, sizes: "256x256", type: "image/jpeg" },
      ],
    });
  }
  navigator.mediaSession.playbackState = isPlaying ? "playing" : "paused";
}

// ─────────────────────────────────────────────────────────────────────────────
// Library — presets / history / recordings
// ─────────────────────────────────────────────────────────────────────────────

function switchLibTab(name, save = true) {
  document.querySelectorAll("#lib-tabs .seg-btn").forEach(t => {
    const active = t.dataset.lib === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
  });
  for (const n of ["presets", "history", "recordings"]) {
    $(`${n}-section`).classList.toggle("hidden", n !== name);
  }
  if (save) localStorage.setItem("squelch.libTab", name);
}

function confirmingDelete(btn, onConfirm) {
  if (btn.dataset.confirming) { onConfirm(); return; }
  btn.dataset.confirming = "1";
  btn.classList.add("confirming");
  const orig = btn.innerHTML;
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14" aria-hidden="true"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/></svg>';
  btn.setAttribute("aria-label", "Confirm delete");
  setTimeout(() => {
    if (btn.dataset.confirming) {
      delete btn.dataset.confirming;
      btn.classList.remove("confirming");
      btn.innerHTML = orig;
      btn.setAttribute("aria-label", "Delete");
    }
  }, 3000);
}

const DEL_ICON = '<svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14" aria-hidden="true"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';

async function loadPresets() {
  const presets = await api("GET", "/presets");
  const list = $("presets-list");
  list.innerHTML = "";
  _presetMarks = [];
  if (!Array.isArray(presets) || !presets.length) {
    list.innerHTML = '<p class="empty-hint">No presets yet — tune a station and save it.</p>';
    drawRuler();
    return;
  }
  for (const p of presets) {
    if (p.band === currentBand || (currentBand === "hd" && p.band === "fm")) {
      _presetMarks.push(p.frequency);
    }
    const unit = BANDS[p.band]?.unit || "MHz";
    const item = document.createElement("div");
    item.className = "lib-item";
    item.setAttribute("role", "button");
    item.setAttribute("tabindex", "0");
    item.setAttribute("aria-label", `Tune to ${p.name}`);
    item.innerHTML = `
      <div class="li-main">
        <span class="li-name">${esc(p.name)}</span>
        <span class="li-sub">${p.frequency} ${unit} · ${esc(p.band.toUpperCase())}</span>
      </div>
      <button class="li-btn" aria-label="Delete preset ${esc(p.name)}">${DEL_ICON}</button>`;
    item.querySelector(".li-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      confirmingDelete(e.currentTarget, async () => { await api("DELETE", `/presets/${p.id}`); loadPresets(); });
    });
    const go = () => tune(p.frequency, p.band);
    item.addEventListener("click", (e) => { if (!e.target.closest(".li-btn")) go(); });
    item.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
    });
    list.appendChild(item);
  }
  drawRuler();
}

async function loadHistory() {
  const items = await api("GET", "/history");
  const list = $("history-list");
  list.innerHTML = "";
  if (!Array.isArray(items) || !items.length) {
    list.innerHTML = '<p class="empty-hint">Nothing heard yet.</p>';
    return;
  }
  for (const h of items) {
    const freq = h.band === "am"
      ? `${Math.round(h.frequency / 1e3)} kHz`
      : `${(h.frequency / 1e6).toFixed(1)} MHz`;
    const musicUrl = (h.artist && h.title)
      ? `https://music.apple.com/search?term=${encodeURIComponent(h.artist + " " + h.title)}`
      : null;

    const row = document.createElement("div");
    row.className = "swipe-row";
    row.innerHTML = `
      <div class="swipe-bg" aria-hidden="true">Delete</div>
      <div class="lib-item">
        <div class="li-main">
          <span class="li-name">${esc(h.station_name || freq)}</span>
          <span class="li-sub">${h.artist && h.title ? esc(h.artist) + " — " + esc(h.title) : freq}</span>
        </div>
        <span class="li-meta">${timeAgo(h.seen_at)}</span>
        ${musicUrl ? `<a class="li-btn" href="${musicUrl}" target="_blank" rel="noopener" aria-label="Open in Apple Music">♫</a>` : ""}
        <button class="li-btn" aria-label="Delete history item">${DEL_ICON}</button>
      </div>`;

    const item = row.querySelector(".lib-item");
    item.addEventListener("click", (e) => {
      if (e.target.closest(".li-btn") || item.dataset.swiped) { item.dataset.swiped = ""; return; }
      const f = h.band === "am" ? Math.round(h.frequency / 1e3) : h.frequency / 1e6;
      tune(f, h.band);
    });
    row.querySelector("button.li-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      confirmingDelete(e.currentTarget, () => deleteHistoryRow(h.id, row));
    });
    addSwipeDelete(row, item, h.id);
    list.appendChild(row);
  }
}

function addSwipeDelete(row, item, id) {
  const bg = row.querySelector(".swipe-bg");
  const REVEAL = 72;
  let x0 = 0, y0 = 0, determined = false, isHoriz = false, revealed = false;

  const snapTo = (open) => {
    item.style.transition = "transform 0.2s ease";
    item.style.transform = open ? `translateX(-${REVEAL}px)` : "translateX(0)";
    bg.style.pointerEvents = open ? "auto" : "none";
    revealed = open;
  };
  item.addEventListener("touchstart", (e) => {
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
    determined = false; isHoriz = false;
    item.style.transition = "none";
  }, { passive: true });
  item.addEventListener("touchmove", (e) => {
    const dx = e.touches[0].clientX - x0, dy = e.touches[0].clientY - y0;
    if (!determined && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
      determined = true; isHoriz = Math.abs(dx) > Math.abs(dy);
    }
    if (!isHoriz) return;
    e.preventDefault();
    item.dataset.swiped = "1";
    const c = revealed ? Math.max(-REVEAL, Math.min(0, dx - REVEAL)) : Math.max(-REVEAL, Math.min(0, dx));
    item.style.transform = `translateX(${c}px)`;
  }, { passive: false });
  item.addEventListener("touchend", (e) => {
    if (!isHoriz) return;
    const dx = e.changedTouches[0].clientX - x0;
    snapTo((revealed ? dx - REVEAL : dx) < -(REVEAL / 2));
    setTimeout(() => { item.dataset.swiped = ""; }, 50);
  });
  bg.style.pointerEvents = "none";
  bg.addEventListener("click", (e) => { e.stopPropagation(); deleteHistoryRow(id, row); });
}

async function deleteHistoryRow(id, row) {
  row.style.height = row.offsetHeight + "px";
  row.style.overflow = "hidden";
  row.style.transition = "height 0.2s ease, opacity 0.15s ease";
  requestAnimationFrame(() => { row.style.height = "0"; row.style.opacity = "0"; });
  await api("DELETE", `/history/${id}`);
  setTimeout(() => {
    row.remove();
    if (!$("history-list").querySelector(".swipe-row")) {
      $("history-list").innerHTML = '<p class="empty-hint">Nothing heard yet.</p>';
    }
  }, 220);
}

async function loadRecordings() {
  const recs = await api("GET", "/recordings");
  const list = $("recordings-list");
  list.innerHTML = "";
  if (!Array.isArray(recs) || !recs.length) {
    list.innerHTML = '<p class="empty-hint">No recordings yet — tap ● while listening.</p>';
    return;
  }
  for (const r of recs) {
    const dur = r.duration_seconds ? formatDuration(r.duration_seconds) : "–";
    const label = r.station_name
      ? `${r.station_name}${r.title ? " – " + r.title : ""}` : r.filename;
    const item = document.createElement("div");
    item.className = "lib-item";
    item.innerHTML = `
      <div class="li-main">
        <span class="li-name" title="${esc(r.filename)}">${esc(label)}</span>
        <span class="li-sub">${dur}</span>
      </div>
      <button class="li-btn" aria-label="Play ${esc(label)}">
        <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <a class="li-btn" href="/recordings/${r.id}/download" download="${esc(r.filename)}" aria-label="Download">↓</a>
      <button class="li-btn" aria-label="Delete ${esc(label)}">${DEL_ICON}</button>`;
    const btns = item.querySelectorAll("button.li-btn");
    btns[0].addEventListener("click", (e) => { e.stopPropagation(); playRecording(r.id, label); });
    btns[1].addEventListener("click", (e) => {
      e.stopPropagation();
      confirmingDelete(e.currentTarget, async () => { await api("DELETE", `/recordings/${r.id}`); loadRecordings(); });
    });
    list.appendChild(item);
  }
}

function playRecording(id, label) {
  player.src = `/recordings/${id}/download`;
  player.play().then(() => setPlayState(true)).catch(() => {});
  $("station-name").textContent = label || "Recording";
  $("track-title").textContent = "Playing recording";
  $("track-title").classList.remove("muted");
  $("track-artist").classList.add("hidden");
}

// ─────────────────────────────────────────────────────────────────────────────
// Recording
// ─────────────────────────────────────────────────────────────────────────────

function setRecordingUi(recording, startedAt) {
  isRecording = recording;
  const btn = $("btn-record");
  btn.classList.toggle("recording", recording);
  btn.setAttribute("aria-pressed", String(recording));
  btn.setAttribute("aria-label", recording ? "Stop recording" : "Start recording");
  clearInterval(_recTimer);
  _recTimer = null;
  if (recording) {
    _recStart = startedAt ? startedAt * 1000 : Date.now();
    _recTimer = setInterval(() => {
      $("rec-elapsed").textContent = formatDuration((Date.now() - _recStart) / 1000);
    }, 1000);
  } else {
    $("rec-elapsed").textContent = "";
  }
}

async function syncRecordingState() {
  const s = await api("GET", "/record/status");
  if (!s.error) setRecordingUi(!!s.recording, s.started_at);
}

$("btn-record").addEventListener("click", async () => {
  if (isRecording) {
    await api("POST", "/record/stop");
    setRecordingUi(false);
    loadRecordings();
    showToast("Recording saved");
  } else {
    const res = await api("POST", "/record/start");
    if (!res.error) {
      setRecordingUi(true, res.started_at);
      showToast("Recording started");
    }
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Modal — save preset
// ─────────────────────────────────────────────────────────────────────────────

const modalPreset = $("modal-preset");
let _modalFocusReturn = null;
const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

function openModal() {
  _modalFocusReturn = document.activeElement;
  modalPreset.classList.remove("hidden");
  $("preset-name-input").focus();
  modalPreset.addEventListener("keydown", trapFocus);
}
function closeModal() {
  modalPreset.classList.add("hidden");
  modalPreset.removeEventListener("keydown", trapFocus);
  _modalFocusReturn?.focus();
}
function trapFocus(e) {
  if (e.key === "Escape") { e.preventDefault(); closeModal(); return; }
  if (e.key !== "Tab") return;
  const els = Array.from(modalPreset.querySelectorAll(FOCUSABLE));
  if (!els.length) return;
  const first = els[0], last = els[els.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

$("btn-save-preset").addEventListener("click", () => {
  const name = $("station-name").textContent;
  $("preset-name-input").value = name !== "Squelch" ? name : "";
  openModal();
});
$("btn-preset-cancel").addEventListener("click", closeModal);
$("btn-preset-save").addEventListener("click", async () => {
  const name = $("preset-name-input").value.trim();
  if (!name) return;
  await api("POST", "/presets", {
    name, frequency: snap(displayFreq), band: currentBand,
    gain: "auto", stereo_mode: "auto",
  });
  closeModal();
  loadPresets();
  showToast("Preset saved");
});
$("preset-name-input").addEventListener("keydown", e => { if (e.key === "Enter") $("btn-preset-save").click(); });
modalPreset.addEventListener("click", e => { if (e.target === modalPreset) closeModal(); });

// ─────────────────────────────────────────────────────────────────────────────
// Phone tabs + adaptive-contrast chrome
// ─────────────────────────────────────────────────────────────────────────────

function activatePanel(name, save = true) {
  document.body.dataset.panel = name;
  document.querySelectorAll("#tabbar .tab").forEach(t => {
    const active = t.dataset.panel === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
  });
  if (name === "radio") drawRuler();
  if (save) localStorage.setItem("squelch.panel", name);
}

document.querySelectorAll("#tabbar .tab").forEach(t =>
  t.addEventListener("click", () => activatePanel(t.dataset.panel)));

// Floating chrome raises its contrast when content scrolls beneath it
function setupAdaptiveContrast() {
  const update = () => {
    const scrolled = (window.scrollY || $("radio").scrollTop || 0) > 8;
    $("tabbar").classList.toggle("is-scrolled", scrolled);
  };
  window.addEventListener("scroll", update, { passive: true });
  $("radio").addEventListener("scroll", update, { passive: true });
}

// ─────────────────────────────────────────────────────────────────────────────
// Keyboard shortcuts
// ─────────────────────────────────────────────────────────────────────────────

document.addEventListener("keydown", (e) => {
  const tag = document.activeElement?.tagName?.toLowerCase();
  if (tag === "input" || tag === "textarea" || document.activeElement?.isContentEditable) return;
  if (document.activeElement === rulerCanvas) return;   // ruler handles its own arrows

  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault();
    const dir = e.key === "ArrowLeft" ? -1 : 1;
    setDisplayFreq(snap(displayFreq + dir * BANDS[currentBand].step));  // debounced commit
  }
  if (e.key === " ") {
    e.preventDefault();
    if (isPlaying) { player.pause(); setPlayState(false); } else { _startStream(); }
  }
  if (e.key === "r" || e.key === "R") { e.preventDefault(); $("btn-record").click(); }
});

// ─────────────────────────────────────────────────────────────────────────────
// Wiring + init
// ─────────────────────────────────────────────────────────────────────────────

document.querySelectorAll("#band-tabs .seg-btn").forEach(tab =>
  tab.addEventListener("click", () => setBand(tab.dataset.band)));

document.querySelectorAll("#lib-tabs .seg-btn").forEach(tab =>
  tab.addEventListener("click", () => switchLibTab(tab.dataset.lib)));

$("btn-play").addEventListener("click", () => {
  if (isPlaying) { player.pause(); setPlayState(false); } else { _startStream(); }
});
player.addEventListener("play",    () => setPlayState(true));
player.addEventListener("playing", () => setPlayState(true));
player.addEventListener("pause",   () => setPlayState(false));
player.addEventListener("ended",   () => setPlayState(false));
player.addEventListener("error",   () => setPlayState(false));

$("volume").addEventListener("input", () => { player.volume = parseFloat($("volume").value); });

setupStepButton($("btn-step-down"), -1);
setupStepButton($("btn-step-up"),   +1);
setupRuler();
buildWxPanel();
buildKeypad();
setupSquelch();
setupAdaptiveContrast();

setGlass(localStorage.getItem("squelch.glass") || "regular", false);
setAutoHd(localStorage.getItem("squelch.autohd") === "1", false);
switchLibTab(localStorage.getItem("squelch.libTab") || "presets", false);
activatePanel(localStorage.getItem("squelch.panel") || "radio", false);
// Start on the last band/frequency used on this device; the first
// WebSocket state message overrides both if the radio is already live.
setBand(localStorage.getItem("squelch.band") || "fm", { retune: false });

connectWs();
loadPresets();
loadHistory();
loadRecordings();
syncRecordingState();

// Keep recording state honest across devices / tab restores
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) syncRecordingState();
});

window.addEventListener("resize", () => drawRuler());
// Redraw once system fonts are ready — the first paint can land before
// font metrics settle, leaving the canvas labels mispositioned or blank.
if (document.fonts?.ready) document.fonts.ready.then(() => drawRuler());
