/* Squelch frontend — vanilla JS, no build step */

// ─────────────────────────────────────────────────────────────────────────────
// Band configuration
// ─────────────────────────────────────────────────────────────────────────────

const BAND_RANGES = {
  fm: { min: 87.5,   max: 108.0,  step: 0.1,   unit: "MHz" },
  hd: { min: 87.5,   max: 108.0,  step: 0.1,   unit: "MHz" },
  wx: { min: 162.4,  max: 162.55, step: 0.025, unit: "MHz" },
};

const WX_CHANNELS = [
  { name: "WX1", freq: 162.550 },
  { name: "WX2", freq: 162.400 },
  { name: "WX3", freq: 162.475 },
  { name: "WX4", freq: 162.425 },
  { name: "WX5", freq: 162.450 },
  { name: "WX6", freq: 162.500 },
  { name: "WX7", freq: 162.525 },
];

// Phone panel → column ID mapping (must match HTML)
const PANELS    = ["now-playing", "tune", "library"];
const PANEL_COL = { "now-playing": "left-col", "tune": "center-col", "library": "right-col" };

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────

let currentBand = "fm";
let currentFreq = 91.1;
let isPlaying   = false;
let isRecording = false;
let ws = null;
let wsReconnectTimer = null;

// Art / metadata
let _prevHasArt     = false;
let _prevArtUrl     = "";
let _prevArtVersion = -1;
let _prevHdLocked   = false;
let _currentAppleMusicUrl = null;

// History auto-refresh
let _prevTrackKey       = "";
let _historyRefreshTimer = null;

// Media Session
let _lastMeta = null;
let _mediaSessionReady = false;

// Recording timer
let _recTimer = null;
let _recStart = null;

// ─────────────────────────────────────────────────────────────────────────────
// Element references
// ─────────────────────────────────────────────────────────────────────────────

const player     = document.getElementById("player");
const btnPlay    = document.getElementById("btn-play");
const iconPlay   = document.getElementById("icon-play");
const iconStop   = document.getElementById("icon-stop");
const btnRecord  = document.getElementById("btn-record");
const recElapsed = document.getElementById("rec-elapsed");
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
const elTrackArtist   = document.getElementById("track-artist");
const elTrackTitle    = document.getElementById("track-title");
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

// ─────────────────────────────────────────────────────────────────────────────
// Band / tuner setup
// ─────────────────────────────────────────────────────────────────────────────

function setBand(band) {
  currentBand = band;
  const r = BAND_RANGES[band];

  // Update all band tab instances (toolbar + panel)
  document.querySelectorAll(".band-tab").forEach(t => {
    const active = t.dataset.band === band;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", active);
  });

  // Update tuner section data attribute (drives CSS show/hide of FM vs WX controls)
  const tunerSection = document.getElementById("tuner-section");
  if (tunerSection) tunerSection.dataset.band = band;

  // Clear HD sub-channel selector when leaving HD band
  const hdChDiv = document.getElementById("hd-channels");
  if (hdChDiv && band !== "hd") {
    hdChDiv.innerHTML = "";
    hdChDiv.classList.add("hidden");
  }

  // Update dial / freq-input constraints for FM/HD
  if (dial) {
    dial.min  = r.min;
    dial.max  = r.max;
    dial.step = r.step;
  }
  if (freqUnit)  freqUnit.textContent  = r.unit;
  if (freqInput) {
    freqInput.step        = r.step;
    freqInput.placeholder = `${r.min}–${r.max} ${r.unit}`;
  }

  buildDialTicks(r);
  buildWxChannels();
  setFreq(clamp(currentFreq, r.min, r.max));
  drawFreqStrip();
}

function buildDialTicks(r) {
  if (!dialTicks) return;
  dialTicks.innerHTML = "";
  // Only show endpoints — frequency strip canvas provides the detailed visualization
  [r.min, r.max].forEach(v => {
    const s = document.createElement("span");
    s.textContent = formatFreq(v, currentBand) + " " + r.unit;
    dialTicks.appendChild(s);
  });
}

function buildWxChannels() {
  const container = document.getElementById("wx-channels");
  if (!container) return;
  if (currentBand !== "wx") { container.innerHTML = ""; return; }

  container.innerHTML = "";
  WX_CHANNELS.forEach(({ name, freq }) => {
    const btn = document.createElement("button");
    btn.className = "wx-ch-btn";
    btn.dataset.freq = freq;
    btn.setAttribute("aria-label", `${name} — ${freq} MHz`);
    btn.innerHTML = `<span class="wx-ch-name">${name}</span><span class="wx-ch-freq">${freq.toFixed(3)}</span>`;
    btn.addEventListener("click", () => tune(freq, "wx"));
    container.appendChild(btn);
  });
  updateWxActive();
}

function updateWxActive() {
  if (currentBand !== "wx") return;
  document.querySelectorAll(".wx-ch-btn").forEach(btn => {
    const match = Math.abs(parseFloat(btn.dataset.freq) - currentFreq) < 0.001;
    btn.classList.toggle("active", match);
    btn.setAttribute("aria-pressed", match);
  });
}

function setFreq(f) {
  currentFreq = f;
  if (dial)      dial.value = f;
  if (freqValue) freqValue.textContent = formatFreq(f, currentBand);
  if (freqInput) freqInput.value = f;
  drawFreqStrip();
  updateWxActive();
}

function formatFreq(f, band) {
  if (band === "wx") return parseFloat(f).toFixed(3);
  return parseFloat(f).toFixed(band === "scanner" ? 3 : 1);
}

function clamp(v, min, max) {
  return Math.min(Math.max(Number(v), min), max);
}

// ─────────────────────────────────────────────────────────────────────────────
// Tuning
// ─────────────────────────────────────────────────────────────────────────────

async function tune(freq, band, hd_channel) {
  band = band || currentBand;
  setFreq(freq);
  setBandIfChanged(band);

  // Start stream inside the user-gesture call stack — satisfies Safari autoplay policy.
  _startStream();

  const body = {
    frequency:   freq,
    band:        band,
    gain:        "auto",
    stereo_mode: "auto",
  };
  if (hd_channel != null) body.hd_channel = hd_channel;

  const res = await api("POST", "/tune", body);
  if (res.error) {
    elStationName.textContent = "Error";
  }
}

function _startStream() {
  player.src   = "/stream?" + Date.now();   // cache-bust forces a fresh connection each play
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
        elTrackTitle.textContent = "Tap ▶ to start";
        elTrackTitle.classList.add("muted");
        elTrackArtist.textContent = "";
        elTrackArtist.classList.add("hidden");
      }
    });
}

function setBandIfChanged(band) {
  if (band !== currentBand) setBand(band);
}

function setPlayState(playing) {
  isPlaying = playing;
  if (iconPlay) iconPlay.classList.toggle("hidden",  playing);
  if (iconStop) iconStop.classList.toggle("hidden", !playing);
  btnPlay.setAttribute("aria-pressed", playing);
  btnPlay.setAttribute("aria-label",   playing ? "Stop" : "Play");

  const miniBtn       = document.getElementById("mini-btn-play");
  const miniIconPlay  = document.getElementById("mini-icon-play");
  const miniIconPause = document.getElementById("mini-icon-pause");
  if (miniBtn)       { miniBtn.setAttribute("aria-pressed", playing); miniBtn.setAttribute("aria-label", playing ? "Stop" : "Play"); }
  if (miniIconPlay)  miniIconPlay.classList.toggle("hidden",  playing);
  if (miniIconPause) miniIconPause.classList.toggle("hidden", !playing);

  updateMediaSession(null);
}

// ─────────────────────────────────────────────────────────────────────────────
// Frequency step buttons (with long-press continuous stepping)
// ─────────────────────────────────────────────────────────────────────────────

function setupStepButton(btn, direction) {
  if (!btn) return;
  let holdTimer = null, holdInterval = null;

  function stepOne() {
    const r = BAND_RANGES[currentBand];
    const raw = currentFreq + direction * r.step;
    setFreq(clamp(parseFloat(raw.toFixed(4)), r.min, r.max));
  }

  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    stepOne();
    holdTimer = setTimeout(() => { holdInterval = setInterval(stepOne, 130); }, 450);
  });

  const commit = () => {
    clearTimeout(holdTimer);
    clearInterval(holdInterval);
    holdTimer = holdInterval = null;
    tune(currentFreq);
  };

  btn.addEventListener("pointerup",     commit);
  btn.addEventListener("pointercancel", () => { clearTimeout(holdTimer); clearInterval(holdInterval); });
  btn.addEventListener("pointerleave",  () => { clearTimeout(holdTimer); clearInterval(holdInterval); });
  btn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); stepOne(); tune(currentFreq); }
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// WebSocket — live metadata
// ─────────────────────────────────────────────────────────────────────────────

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

// ─────────────────────────────────────────────────────────────────────────────
// Art crossfade + dominant color extraction
// ─────────────────────────────────────────────────────────────────────────────

function updateArt(rawUrl) {
  const hasNewArt = rawUrl !== "/static/placeholder.svg";
  const src = hasNewArt ? rawUrl + "?t=" + Date.now() : "/static/placeholder.svg";

  artBack.src = elArt.src || "/static/placeholder.svg";

  // Begin bloom: scale-down + opacity-0 via class
  elArt.classList.add("art-loading");

  artBlurBg.style.backgroundImage = hasNewArt ? `url("${src}")` : "none";

  const img = new Image();
  img.onload = () => {
    elArt.src = img.src;
    // Remove class on next frame so CSS transition fires
    requestAnimationFrame(() => elArt.classList.remove("art-loading"));

    const miniArt = document.getElementById("mini-art");
    if (miniArt) miniArt.src = img.src;

    const root = document.documentElement;
    if (hasNewArt) {
      const color = extractDominantColor(img);
      if (color) {
        root.style.setProperty("--accent-dynamic", color);
        root.style.setProperty("--accent-glow", color.replace("rgb", "rgba").replace(")", ", 0.25)"));

        // Ambient background color components
        const m = color.match(/rgb\((\d+),(\d+),(\d+)\)/);
        if (m) {
          root.style.setProperty("--art-r", m[1]);
          root.style.setProperty("--art-g", m[2]);
          root.style.setProperty("--art-b", m[3]);
        }
      }
    } else {
      root.style.setProperty("--accent-dynamic", "var(--accent)");
      root.style.setProperty("--accent-glow", "rgba(224, 92, 0, 0.25)");
      root.style.setProperty("--art-r", "224");
      root.style.setProperty("--art-g", "92");
      root.style.setProperty("--art-b", "0");
    }
  };
  img.onerror = () => {
    elArt.src = "/static/placeholder.svg";
    elArt.classList.remove("art-loading");
    artBlurBg.style.backgroundImage = "none";
    const root = document.documentElement;
    root.style.setProperty("--accent-dynamic", "var(--accent)");
    root.style.setProperty("--accent-glow", "rgba(224, 92, 0, 0.25)");
    root.style.setProperty("--art-r", "224");
    root.style.setProperty("--art-g", "92");
    root.style.setProperty("--art-b", "0");
  };
  img.src = src;
}

function extractDominantColor(imgEl) {
  try {
    const canvas = document.getElementById("color-canvas");
    if (!canvas) return null;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(imgEl, 0, 0, 8, 8);
    const data = ctx.getImageData(0, 0, 8, 8).data;
    let r = 0, g = 0, b = 0, n = 0;
    for (let i = 0; i < data.length; i += 4) {
      const lum = data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
      if (lum > 20 && lum < 220) { r += data[i]; g += data[i + 1]; b += data[i + 2]; n++; }
    }
    if (!n) return null;
    r = Math.round(r / n);
    g = Math.round(g / n);
    b = Math.round(b / n);

    // Boost saturation toward dominant channel
    const mx = Math.max(r, g, b), k = 1.5;
    r = Math.min(255, Math.round(r * (r === mx ? k : 1 / k)));
    g = Math.min(255, Math.round(g * (g === mx ? k : 1 / k)));
    b = Math.min(255, Math.round(b * (b === mx ? k : 1 / k)));

    // Enforce readable luminance
    const toLinear = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    const lum = 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
    const isLight = window.matchMedia("(prefers-color-scheme: light)").matches;

    if (isLight) {
      if (lum > 0.35) {
        const s = 0.45 / lum;
        r = Math.round(r * s); g = Math.round(g * s); b = Math.round(b * s);
      }
    } else {
      if (lum < 0.08) {
        const s = 0.18 / Math.max(lum, 0.001);
        r = Math.min(255, Math.round(r * s));
        g = Math.min(255, Math.round(g * s));
        b = Math.min(255, Math.round(b * s));
      }
    }

    return `rgb(${r},${g},${b})`;
  } catch {
    return null;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Metadata from WebSocket
// ─────────────────────────────────────────────────────────────────────────────

function applyMeta(m) {
  _lastMeta = m;

  // Station name + page title
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

  // Track info
  const trackKey = `${m.artist || ""}|${m.title || ""}`;
  if (m.artist && m.title) {
    elTrackArtist.textContent = m.artist;
    elTrackArtist.classList.remove("hidden");
    elTrackTitle.textContent = m.title;
    elTrackTitle.classList.remove("muted");
  } else if (m.title) {
    elTrackArtist.textContent = "";
    elTrackArtist.classList.add("hidden");
    elTrackTitle.textContent = m.title;
    elTrackTitle.classList.remove("muted");
  } else {
    const hint = {
      idle:      "Ready to tune",
      tuning:    "Tuning…",
      buffering: "Buffering…",
      live:      m.band === "fm" || m.band === "hd" ? "Waiting for RDS…" : "Live",
      error:     "Radio error — try retuning",
    }[m.state] || "";
    elTrackArtist.textContent = "";
    elTrackArtist.classList.add("hidden");
    elTrackTitle.textContent = hint;
    elTrackTitle.classList.add("muted");
  }

  // Track change animation
  if (trackKey !== _prevTrackKey && (m.artist || m.title)) {
    _prevTrackKey = trackKey;
    const nowInfo = document.getElementById("now-info");
    if (nowInfo) {
      nowInfo.classList.remove("track-changing");
      void nowInfo.offsetWidth;
      nowInfo.classList.add("track-changing");
      nowInfo.addEventListener("animationend", () => nowInfo.classList.remove("track-changing"), { once: true });
    }
    clearTimeout(_historyRefreshTimer);
    _historyRefreshTimer = setTimeout(loadHistory, 6000);
  }

  // Cover art crossfade
  const nowHasArt  = !!(m.has_art && m.art_url);
  const artVersion = m.art_version ?? -1;
  if (nowHasArt !== _prevHasArt || (nowHasArt && m.art_url !== _prevArtUrl) || (nowHasArt && artVersion !== _prevArtVersion)) {
    _prevHasArt     = nowHasArt;
    _prevArtUrl     = m.art_url || "";
    _prevArtVersion = artVersion;
    updateArt(nowHasArt ? m.art_url : "/static/placeholder.svg");
  }

  // Apple Music link
  _currentAppleMusicUrl = m.apple_music_url || null;
  updateArtLink(m);

  // HD badge + lock-in animation
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
  const signalMeter  = document.getElementById("signal-meter");
  if (signalMeter) {
    signalMeter.setAttribute("aria-label", `Signal: ${signalLabels[b] || "No signal"}`);
    signalMeter.dataset.signalLabel = signalLabels[b] || "No signal";
    signalMeter.classList.toggle("strong", b >= 4);
  }
  bars.forEach(bar => bar.classList.toggle("active", Number(bar.dataset.n) <= b));
  document.querySelectorAll(".mini-bar").forEach(bar => bar.classList.toggle("active", Number(bar.dataset.n) <= b));

  // HD sub-channel selector
  const hdChDiv = document.getElementById("hd-channels");
  if (hdChDiv) {
    if (m.hd_locked && Array.isArray(m.hd_channels_available) && m.hd_channels_available.length > 1) {
      hdChDiv.innerHTML = m.hd_channels_available.map(ch => {
        const active = ch === (m.hd_channel || 1);
        return `<button class="hd-ch-btn${active ? " active" : ""}" data-ch="${ch}" aria-label="HD channel ${ch}" aria-pressed="${active}">${ch}</button>`;
      }).join("");
      hdChDiv.querySelectorAll(".hd-ch-btn").forEach(btn => {
        btn.addEventListener("click", () => tune(currentFreq, currentBand, +btn.dataset.ch));
      });
      hdChDiv.classList.remove("hidden");
    } else {
      hdChDiv.classList.add("hidden");
    }
  }

  // Diagnostics
  if (m.diag) applyDiag(m.diag, m.band);

  // Media Session metadata
  updateMediaSession(m);

  // Mini-player info sync
  syncMiniPlayer();
}

function updateArtLink(m) {
  let url = _currentAppleMusicUrl;
  if (!url && m.artist && m.title) {
    url = `https://music.apple.com/search?term=${encodeURIComponent(m.artist + " " + m.title)}`;
  }
  if (url) {
    elArtWrap.classList.add("has-link");
    elArtWrap.setAttribute("role", "link");
    elArtWrap.setAttribute("aria-label", "Open in Apple Music");
    elArtWrap.setAttribute("tabindex", "0");
    elArtWrap.onclick = () => window.open(url, "_blank", "noopener");
  } else {
    elArtWrap.classList.remove("has-link");
    elArtWrap.removeAttribute("role");
    elArtWrap.removeAttribute("aria-label");
    elArtWrap.removeAttribute("tabindex");
    elArtWrap.onclick = null;
  }
}

function toggleEl(el, show) {
  el.classList.toggle("hidden", !show);
}

// ─────────────────────────────────────────────────────────────────────────────
// Diagnostics panel
// ─────────────────────────────────────────────────────────────────────────────

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
  bar.className   = "diag-fill " + colorFn(value);
  val.textContent = value > 0 ? labelFn(value) : "—";
}

document.getElementById("btn-copy-diag")?.addEventListener("click", () => {
  const info = { timestamp: new Date().toISOString(), frequency: currentFreq, band: currentBand, ...(_lastDiag || {}) };
  const text = JSON.stringify(info, null, 2);
  const btn  = document.getElementById("btn-copy-diag");
  const orig = btn.textContent;
  const finish = () => { btn.textContent = "Copied!"; setTimeout(() => btn.textContent = orig, 1500); };
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
  ta.focus(); ta.select();
  try { document.execCommand("copy"); done(); } catch (_) {}
  document.body.removeChild(ta);
}

// ─────────────────────────────────────────────────────────────────────────────
// iOS / desktop Media Session API
// ─────────────────────────────────────────────────────────────────────────────

function setupMediaSession() {
  if (!("mediaSession" in navigator) || _mediaSessionReady) return;
  _mediaSessionReady = true;

  navigator.mediaSession.setActionHandler("play", () => _startStream());
  navigator.mediaSession.setActionHandler("pause", () => { player.pause(); setPlayState(false); });
  navigator.mediaSession.setActionHandler("stop",  () => { player.pause(); setPlayState(false); });
  navigator.mediaSession.setActionHandler("previoustrack", () => {
    const r = BAND_RANGES[currentBand];
    tune(clamp(parseFloat((currentFreq - r.step).toFixed(4)), r.min, r.max));
  });
  navigator.mediaSession.setActionHandler("nexttrack", () => {
    const r = BAND_RANGES[currentBand];
    tune(clamp(parseFloat((currentFreq + r.step).toFixed(4)), r.min, r.max));
  });

  if (_lastMeta) updateMediaSession(_lastMeta);
}

function updateMediaSession(m) {
  if (!("mediaSession" in navigator)) return;

  if (m !== null) {
    const artSrc = (m?.has_art && m?.art_url)
      ? location.origin + m.art_url + "?t=" + (m.art_version || 0)
      : location.origin + "/static/placeholder.svg";

    const titleText  = m?.title        || (freqValue?.textContent ?? "") + " " + (freqUnit?.textContent ?? "");
    const artistText = m?.artist       || m?.station_name || "";
    const albumText  = m?.station_name || m?.slogan       || "Squelch";

    navigator.mediaSession.metadata = new MediaMetadata({
      title:   titleText,
      artist:  artistText,
      album:   albumText,
      artwork: [
        { src: artSrc, sizes: "512x512", type: "image/jpeg" },
        { src: artSrc, sizes: "256x256", type: "image/jpeg" },
      ],
    });
  }

  navigator.mediaSession.playbackState = isPlaying ? "playing" : "paused";
}

// ─────────────────────────────────────────────────────────────────────────────
// Mini-player
// ─────────────────────────────────────────────────────────────────────────────

function setupMiniPlayer() {
  const nowPlaying = document.getElementById("now-playing");
  const miniPlayer = document.getElementById("mini-player");
  if (!nowPlaying || !miniPlayer) return;

  const obs = new IntersectionObserver(entries => {
    const visible = entries[0].isIntersecting;
    miniPlayer.classList.toggle("hidden", visible);
    if (!visible) syncMiniPlayer();
  }, { threshold: 0.15 });

  obs.observe(nowPlaying);

  document.getElementById("mini-btn-play")?.addEventListener("click", () => {
    if (isPlaying) { player.pause(); setPlayState(false); } else { _startStream(); }
  });
}

function syncMiniPlayer() {
  const miniPlayer = document.getElementById("mini-player");
  if (!miniPlayer || miniPlayer.classList.contains("hidden")) return;

  const miniArt     = document.getElementById("mini-art");
  const miniStation = document.getElementById("mini-station");
  const miniTrack   = document.getElementById("mini-track");

  if (miniArt)     miniArt.src = elArt.src;
  if (miniStation) miniStation.textContent = elStationName.textContent;
  if (miniTrack)   miniTrack.textContent   = elTrackTitle?.textContent || "";
}

// ─────────────────────────────────────────────────────────────────────────────
// Bottom nav / panel switching (phone)
// ─────────────────────────────────────────────────────────────────────────────

function activateTab(name) {
  const app = document.getElementById("app");
  const currentPanel = app.dataset.panel || "now-playing";
  if (name === currentPanel) return;

  const prevIdx = PANELS.indexOf(currentPanel);
  const nextIdx = PANELS.indexOf(name);
  if (nextIdx === -1) return;
  const dir = nextIdx > prevIdx ? 1 : -1;
  const dur = 240;

  document.querySelectorAll(".nav-tab").forEach(t => {
    const active = t.dataset.panel === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
  });

  const outColId = PANEL_COL[currentPanel];
  const inColId  = PANEL_COL[name];
  const outCol   = document.getElementById(outColId);
  const inCol    = document.getElementById(inColId);
  if (!inCol) return;

  if (outCol) {
    outCol.style.animation = `${dir > 0 ? "panel-exit-left" : "panel-exit-right"} ${dur}ms var(--ease-smooth) forwards`;
  }

  inCol.style.display   = "flex";
  inCol.style.opacity   = "0";
  inCol.style.animation = "";

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      inCol.style.animation = `${dir > 0 ? "panel-enter-right" : "panel-enter-left"} ${dur}ms var(--ease-smooth) forwards`;
    });
  });

  setTimeout(() => {
    if (outCol) { outCol.style.animation = ""; outCol.style.display = "none"; outCol.style.opacity = ""; }
    app.dataset.panel = name;
    saveLayoutPref("lastTab", name);
  }, dur);
}

function initPanels() {
  if (window.innerWidth >= 640) return;

  const leftCol   = document.getElementById("left-col");
  const centerCol = document.getElementById("center-col");
  const rightCol  = document.getElementById("right-col");
  if (leftCol)   leftCol.style.display   = "flex";
  if (centerCol) centerCol.style.display = "none";
  if (rightCol)  rightCol.style.display  = "none";
}

// ─────────────────────────────────────────────────────────────────────────────
// Library tab switching (Presets / History / Recordings)
// ─────────────────────────────────────────────────────────────────────────────

function switchLibTab(name, save = true) {
  const sectionMap = {
    presets:    document.getElementById("presets-section"),
    history:    document.getElementById("history-section"),
    recordings: document.getElementById("recordings-section"),
  };

  document.querySelectorAll(".lib-tab").forEach(t => {
    const active = t.dataset.lib === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
  });

  Object.entries(sectionMap).forEach(([n, el]) => {
    if (el) el.classList.toggle("hidden", n !== name);
  });

  if (save) saveLayoutPref("lastLibTab", name);
}

function initLibTabs() {
  document.querySelectorAll(".lib-tab").forEach(tab => {
    tab.addEventListener("click", () => switchLibTab(tab.dataset.lib));
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Frequency strip canvas
// ─────────────────────────────────────────────────────────────────────────────

let _stripDragging = false;

function drawFreqStrip() {
  const canvas = document.getElementById("freq-strip");
  if (!canvas || canvas.offsetWidth === 0) return;

  const dpr = window.devicePixelRatio || 1;
  const W   = canvas.offsetWidth;
  const H   = canvas.offsetHeight;

  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const r     = BAND_RANGES[currentBand];
  const range = r.max - r.min;
  const cs    = getComputedStyle(document.documentElement);

  // Background
  ctx.fillStyle = cs.getPropertyValue("--surface2").trim() || "rgba(255,255,255,0.085)";
  if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(0, 0, W, H, 8); ctx.fill(); }
  else { ctx.fillRect(0, 0, W, H); }

  // Per-band tick config — minor ticks are visual reference without labels,
  // major ticks are labeled.  Aim for ~4-6 major labels across the strip.
  let minorStep, majorStep;
  if      (currentBand === "wx")       { minorStep = 0.025; majorStep = 0.05; }
  else if (currentBand === "am")       { minorStep = 100;   majorStep = 200;  }
  else if (currentBand === "scanner")  { minorStep = 50;    majorStep = 200;  }
  else                                 { minorStep = 1;     majorStep = 5;    } // FM / HD

  const eps = minorStep * 0.01;  // float tolerance for loop termination

  // Minor ticks (short, no label)
  ctx.strokeStyle = "rgba(255,255,255,0.12)";
  ctx.lineWidth   = 1;
  for (let f = Math.ceil(r.min / minorStep) * minorStep; f <= r.max + eps; f += minorStep) {
    if (Math.round(f / majorStep) * majorStep === Math.round(f * 1000) / 1000) continue; // skip major positions
    const x = ((f - r.min) / range) * W;
    ctx.beginPath();
    ctx.moveTo(x, H * 0.68);
    ctx.lineTo(x, H - 2);
    ctx.stroke();
  }

  // Major ticks + labels
  const fontSize = Math.max(8, Math.min(11, H * 0.26));
  ctx.font       = `${fontSize}px -apple-system, sans-serif`;
  ctx.textAlign  = "center";
  ctx.strokeStyle = "rgba(255,255,255,0.22)";
  ctx.lineWidth   = 1;
  ctx.fillStyle   = "rgba(160,160,168,0.75)";

  for (let f = Math.ceil(r.min / majorStep) * majorStep; f <= r.max + eps; f += majorStep) {
    const x = ((f - r.min) / range) * W;
    ctx.beginPath();
    ctx.moveTo(x, H * 0.52);
    ctx.lineTo(x, H - 2);
    ctx.stroke();
    ctx.fillText(formatFreq(f, currentBand), x, H * 0.38);
  }

  // Position marker — vertical line with triangle indicator at bottom
  const markerX     = ((currentFreq - r.min) / range) * W;
  const accentColor = cs.getPropertyValue("--accent-dynamic").trim() || "#e05c00";

  ctx.save();
  ctx.strokeStyle = accentColor;
  ctx.lineWidth   = 2;
  ctx.globalAlpha = 0.8;
  ctx.shadowBlur  = 8;
  ctx.shadowColor = accentColor;
  ctx.beginPath();
  ctx.moveTo(markerX, 0);
  ctx.lineTo(markerX, H);
  ctx.stroke();
  ctx.restore();

  ctx.fillStyle = accentColor;
  ctx.beginPath();
  ctx.moveTo(markerX - 5, H);
  ctx.lineTo(markerX,     H * 0.5);
  ctx.lineTo(markerX + 5, H);
  ctx.closePath();
  ctx.fill();
}

function setupFreqStrip() {
  const canvas = document.getElementById("freq-strip");
  if (!canvas) return;

  const getFreqFromX = (x) => {
    const r = BAND_RANGES[currentBand];
    const W = canvas.offsetWidth;
    return clamp(r.min + (x / W) * (r.max - r.min), r.min, r.max);
  };

  canvas.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    canvas.setPointerCapture(e.pointerId);
    _stripDragging = true;
    const f = getFreqFromX(e.offsetX);
    const decimals = currentBand === "wx" ? 3 : 1;
    setFreq(clamp(parseFloat(f.toFixed(decimals)), BAND_RANGES[currentBand].min, BAND_RANGES[currentBand].max));
  });

  canvas.addEventListener("pointermove", (e) => {
    if (!_stripDragging) return;
    const f = getFreqFromX(e.offsetX);
    const decimals = currentBand === "wx" ? 3 : 1;
    setFreq(clamp(parseFloat(f.toFixed(decimals)), BAND_RANGES[currentBand].min, BAND_RANGES[currentBand].max));
  });

  canvas.addEventListener("pointerup",     () => { if (!_stripDragging) return; _stripDragging = false; tune(currentFreq); });
  canvas.addEventListener("pointercancel", () => { _stripDragging = false; });

  new ResizeObserver(() => drawFreqStrip()).observe(canvas);
}

// ─────────────────────────────────────────────────────────────────────────────
// Layout persistence
// ─────────────────────────────────────────────────────────────────────────────

function getLayoutPrefs() {
  try { return JSON.parse(localStorage.getItem("squelch.layout") || "{}"); } catch { return {}; }
}

function saveLayoutPref(key, value) {
  const p = getLayoutPrefs();
  p[key]  = value;
  localStorage.setItem("squelch.layout", JSON.stringify(p));
}

function loadLayoutPrefs() {
  const p = getLayoutPrefs();

  if (p.lastTab && window.innerWidth < 640) {
    setTimeout(() => activateTab(p.lastTab), 0);
  }

  if (p.lastLibTab) {
    switchLibTab(p.lastLibTab, false);
  }

  if (p.diagOpen && window.innerWidth >= 640) {
    const drawer = document.getElementById("diagnostics-drawer");
    if (drawer) drawer.open = true;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Generic API helper
// ─────────────────────────────────────────────────────────────────────────────

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

// ─────────────────────────────────────────────────────────────────────────────
// Presets
// ─────────────────────────────────────────────────────────────────────────────

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
    item.setAttribute("role", "button");
    item.setAttribute("tabindex", "0");
    item.setAttribute("aria-label", `Tune to ${p.name}`);
    const unit = BAND_RANGES[p.band]?.unit || (p.band === "am" ? "kHz" : "MHz");
    item.innerHTML = `
      <span class="preset-freq">${p.frequency} ${unit}</span>
      <span class="preset-name">${esc(p.name)}</span>
      <span class="preset-band">${p.band}</span>
      <button class="btn-delete" aria-label="Delete preset ${esc(p.name)}" title="Delete">×</button>
    `;
    item.querySelector(".btn-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      const btn = e.currentTarget;
      if (btn.dataset.confirming) {
        await api("DELETE", `/presets/${p.id}`);
        loadPresets();
      } else {
        btn.dataset.confirming = "1";
        btn.innerHTML = _confirmIcon();
        btn.style.color = "#ff453a";
        btn.setAttribute("aria-label", "Confirm delete");
        setTimeout(() => {
          if (btn.dataset.confirming) {
            delete btn.dataset.confirming;
            btn.textContent = "×";
            btn.style.color = "";
            btn.setAttribute("aria-label", `Delete preset ${esc(p.name)}`);
          }
        }, 3000);
      }
    });
    const doTune = (e) => {
      if (!e.target.closest(".btn-delete")) tune(p.frequency, p.band);
    };
    item.addEventListener("click", doTune);
    item.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); doTune(e); } });
    presetsList.appendChild(item);
  });
}

async function saveCurrentPreset(name) {
  await api("POST", "/presets", {
    name,
    frequency:   currentFreq,
    band:        currentBand,
    gain:        "auto",
    bandwidth:   "wide",
    stereo_mode: "auto",
  });
  loadPresets();
}

// ─────────────────────────────────────────────────────────────────────────────
// Recordings
// ─────────────────────────────────────────────────────────────────────────────

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
    const dur   = r.duration_seconds ? formatDuration(r.duration_seconds) : "–";
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
      const btn = e.currentTarget;
      if (btn.dataset.confirming) {
        await api("DELETE", `/recordings/${r.id}`);
        loadRecordings();
      } else {
        btn.dataset.confirming = "1";
        btn.innerHTML = _confirmIcon();
        btn.style.color = "#ff453a";
        btn.setAttribute("aria-label", "Confirm delete");
        setTimeout(() => {
          if (btn.dataset.confirming) {
            delete btn.dataset.confirming;
            btn.textContent = "×";
            btn.style.color = "";
            btn.setAttribute("aria-label", `Delete ${esc(label)}`);
          }
        }, 3000);
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
  elTrackTitle.textContent  = "Playing recording";
  elTrackTitle.classList.remove("muted");
  elTrackArtist.textContent = "";
  elTrackArtist.classList.add("hidden");
  syncMiniPlayer();
}

function formatDuration(s) {
  const m = Math.floor(s / 60), sec = s % 60;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// History
// ─────────────────────────────────────────────────────────────────────────────

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
    const freqBadge = h.station_name ? `<span class="history-freq">${freq}</span>` : "";

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
          ${h.artist && h.title ? `<span class="history-track">${esc(h.artist)} — ${esc(h.title)}</span>` : ""}
        </div>
        ${musicUrl ? `<a class="btn-music" href="${musicUrl}" target="_blank" rel="noopener" aria-label="Open in Apple Music" title="Open in Apple Music">♫</a>` : ""}
        <button class="btn-history-del" aria-label="Delete history item" title="Delete">
          <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14" aria-hidden="true"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
        </button>
      </div>
    `;

    const item = row.querySelector(".history-item");

    item.addEventListener("click", (e) => {
      if (e.target.closest(".btn-music") || e.target.closest(".btn-history-del")) return;
      if (item.dataset.swiped) { item.dataset.swiped = ""; return; }
      const tuneFreq = h.band === "am"
        ? Math.round(h.frequency / 1e3)
        : parseFloat(h.frequency / 1e6);
      tune(tuneFreq, h.band);
    });

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
  let x0 = 0, y0 = 0, determined = false, isHoriz = false, revealed = false;

  const snap = (open) => {
    item.style.transition = "transform 0.2s ease";
    item.style.transform  = open ? `translateX(-${REVEAL}px)` : "translateX(0)";
    bg.style.pointerEvents = open ? "auto" : "none";
    revealed = open;
  };

  item.addEventListener("touchstart", (e) => {
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
    determined = false; isHoriz = false;
    item.style.transition = "none";
  }, { passive: true });

  item.addEventListener("touchmove", (e) => {
    const dx = e.touches[0].clientX - x0;
    const dy = e.touches[0].clientY - y0;
    if (!determined && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
      determined = true; isHoriz = Math.abs(dx) > Math.abs(dy);
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
    const dx  = e.changedTouches[0].clientX - x0;
    const net = revealed ? dx - REVEAL : dx;
    snap(net < -(REVEAL / 2));
    setTimeout(() => { item.dataset.swiped = ""; }, 50);
  });

  bg.addEventListener("click", (e) => { e.stopPropagation(); _deleteHistoryRow(id, row); });
}

async function _deleteHistoryRow(id, row) {
  const h = row.offsetHeight;
  row.style.height = h + "px";
  row.style.overflow = "hidden";
  row.style.transition = "height 0.2s ease, opacity 0.15s ease";
  requestAnimationFrame(() => { row.style.height = "0"; row.style.opacity = "0"; });
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
  const diff = Date.now() / 1000 - Number(ts);
  if (diff < 60)    return "just now";
  if (diff < 3600)  return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Column layout — drag-to-reorder + collapsible (desktop ≥1024px)
// ─────────────────────────────────────────────────────────────────────────────

// Visual order of column IDs (left → right on desktop)
let _colOrder     = ["center-col", "left-col", "right-col"];
// Collapsed state for the two side columns
let _colCollapsed = { "center-col": false, "right-col": false };

function initColLayout() {
  if (window.innerWidth < 1024) return;
  const p = getLayoutPrefs();
  if (Array.isArray(p.colOrder) && p.colOrder.length === 3) _colOrder = p.colOrder;
  if (p.colCollapsed && typeof p.colCollapsed === "object") {
    _colCollapsed = { "center-col": false, "right-col": false, ...p.colCollapsed };
  }
  _applyColOrder();
  _applyColCollapsed();
}

function _applyColOrder() {
  _colOrder.forEach((id, i) => {
    const el = document.getElementById(id);
    if (el) el.style.order = i + 1;
  });
}

function _applyColCollapsed() {
  ["center-col", "right-col"].forEach(id => {
    const el  = document.getElementById(id);
    const btn = el?.querySelector(".col-collapse-btn");
    const collapsed = !!_colCollapsed[id];
    if (!el) return;

    el.classList.toggle("col-collapsed", collapsed);
    if (btn) {
      btn.setAttribute("aria-expanded", String(!collapsed));
      btn.setAttribute("aria-label",
        (collapsed ? "Expand" : "Collapse") + " " +
        (id === "center-col" ? "Tuner" : "Library") + " column");
      btn.dataset.collapsed = collapsed ? "true" : "false";
    }
  });
}

function toggleColCollapse(colId) {
  if (!["center-col", "right-col"].includes(colId)) return;
  _colCollapsed[colId] = !_colCollapsed[colId];
  _applyColCollapsed();
  saveLayoutPref("colCollapsed", _colCollapsed);
  // Redraw freq strip after the flex-basis transition completes
  setTimeout(drawFreqStrip, 520);
}

// ── Drag-to-reorder ──────────────────────────────────────────────────────────

let _dragState      = null;
let _flipCleanup    = null;

function setupColDrag() {
  document.querySelectorAll(".col-drag-handle").forEach(handle => {
    handle.addEventListener("pointerdown", _onColDragStart);
  });
}

function _onColDragStart(e) {
  if (window.innerWidth < 1024) return;
  e.preventDefault();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);

  const colId = handle.dataset.col;
  const colEl = document.getElementById(colId);
  if (!colEl) return;

  _dragState = {
    pointerId: e.pointerId,
    colId,
    colEl,
    startX:  e.clientX,
    swapCooldown: false,
    handle,
  };

  colEl.classList.add("col-dragging");
  handle.addEventListener("pointermove",   _onColDragMove);
  handle.addEventListener("pointerup",     _onColDragEnd);
  handle.addEventListener("pointercancel", _onColDragEnd);
}

function _onColDragMove(e) {
  if (!_dragState || e.pointerId !== _dragState.pointerId) return;
  const dx       = e.clientX - _dragState.startX;
  const colWidth = _dragState.colEl.offsetWidth;
  const threshold = Math.max(60, colWidth * 0.35);
  const idx = _colOrder.indexOf(_dragState.colId);

  if (!_dragState.swapCooldown) {
    if (dx < -threshold && idx > 0) {
      _flipSwap(idx, idx - 1);
      _dragState.startX = e.clientX;
      _dragState.swapCooldown = true;
      setTimeout(() => { if (_dragState) _dragState.swapCooldown = false; }, 360);
    } else if (dx > threshold && idx < _colOrder.length - 1) {
      _flipSwap(idx, idx + 1);
      _dragState.startX = e.clientX;
      _dragState.swapCooldown = true;
      setTimeout(() => { if (_dragState) _dragState.swapCooldown = false; }, 360);
    }
  }
}

function _onColDragEnd(e) {
  if (!_dragState || e.pointerId !== _dragState.pointerId) return;
  _dragState.colEl.classList.remove("col-dragging");
  const h = _dragState.handle;
  h.removeEventListener("pointermove",   _onColDragMove);
  h.removeEventListener("pointerup",     _onColDragEnd);
  h.removeEventListener("pointercancel", _onColDragEnd);
  _dragState = null;
}

function _flipSwap(idxA, idxB) {
  const cols = _colOrder.map(id => document.getElementById(id));

  // FIRST: record current positions before mutation
  const beforeRects = cols.map(el => el?.getBoundingClientRect());

  // Mutate order
  [_colOrder[idxA], _colOrder[idxB]] = [_colOrder[idxB], _colOrder[idxA]];
  _applyColOrder();

  // LAST: record new positions (synchronous read forces reflow)
  const afterRects = cols.map(el => el?.getBoundingClientRect());

  // INVERT: push each element back to its old visual position using transforms
  cols.forEach((el, i) => {
    if (!el || !beforeRects[i] || !afterRects[i]) return;
    const dx = beforeRects[i].left - afterRects[i].left;
    if (Math.abs(dx) < 1) return;
    el.style.transition = "none";
    el.style.transform  = `translateX(${dx}px)`;
  });

  // PLAY: next frame removes the inverted transforms so CSS transition animates to final position
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      cols.forEach(el => {
        if (!el) return;
        el.style.transition = "transform 320ms cubic-bezier(0.4, 0, 0.2, 1)";
        el.style.transform  = "";
      });
    });
  });

  // Clean up inline styles after animation
  clearTimeout(_flipCleanup);
  _flipCleanup = setTimeout(() => {
    cols.forEach(el => {
      if (!el) return;
      el.style.transition = "";
      el.style.transform  = "";
    });
    drawFreqStrip();
  }, 340);

  saveLayoutPref("colOrder", _colOrder);
}

// ─────────────────────────────────────────────────────────────────────────────
// Modal — save preset (with focus trap)
// ─────────────────────────────────────────────────────────────────────────────

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
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

// ─────────────────────────────────────────────────────────────────────────────
// Toast notifications
// ─────────────────────────────────────────────────────────────────────────────

function showToast(msg) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add("toast-out");
    setTimeout(() => el.remove(), 200);
  }, 2000);
}

// ─────────────────────────────────────────────────────────────────────────────
// XSS-safe HTML escaping
// ─────────────────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function _confirmIcon() {
  return `<svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14" aria-hidden="true"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/></svg>`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Event wiring
// ─────────────────────────────────────────────────────────────────────────────

// Signal tooltip + long-press for diagnostics on phone
const signalTooltipEl = document.getElementById("signal-tooltip");
const signalMeterEl   = document.getElementById("signal-meter");
let _diagLongPress = null;

signalMeterEl?.addEventListener("mouseenter", () => {
  if (!signalTooltipEl) return;
  const label = signalMeterEl.dataset.signalLabel || "No signal";
  signalTooltipEl.textContent = `Signal: ${label}`;
  const rect = signalMeterEl.getBoundingClientRect();
  signalTooltipEl.style.left = `${rect.left + rect.width / 2}px`;
  signalTooltipEl.style.top  = `${rect.top}px`;
  signalTooltipEl.classList.add("visible");
});
signalMeterEl?.addEventListener("mouseleave", () => signalTooltipEl?.classList.remove("visible"));

// Long-press on signal meter (phone) → open diagnostics in Tune panel
signalMeterEl?.addEventListener("pointerdown", () => {
  _diagLongPress = setTimeout(() => {
    if (window.innerWidth < 640) {
      activateTab("tune");
      setTimeout(() => {
        const drawer = document.getElementById("diagnostics-drawer");
        if (drawer) { drawer.open = true; drawer.scrollIntoView({ behavior: "smooth", block: "start" }); }
      }, 300);
    }
  }, 600);
});
signalMeterEl?.addEventListener("pointerup",     () => clearTimeout(_diagLongPress));
signalMeterEl?.addEventListener("pointermove",   () => clearTimeout(_diagLongPress));
signalMeterEl?.addEventListener("pointercancel", () => clearTimeout(_diagLongPress));

// Band tabs
document.querySelectorAll(".band-tab").forEach(tab => {
  tab.addEventListener("click", () => setBand(tab.dataset.band));
});

// Frequency step buttons
setupStepButton(document.getElementById("btn-step-down"), -1);
setupStepButton(document.getElementById("btn-step-up"),   +1);

// Dial scrub (preview) → commit on release
if (dial) {
  dial.addEventListener("input",  () => setFreq(parseFloat(dial.value)));
  dial.addEventListener("change", () => tune(currentFreq));
}

// Manual frequency input
if (btnGo) {
  btnGo.addEventListener("click", () => {
    const v = parseFloat(freqInput.value);
    if (!isNaN(v)) tune(clamp(v, BAND_RANGES[currentBand].min, BAND_RANGES[currentBand].max));
  });
}
if (freqInput) {
  freqInput.addEventListener("keydown", e => { if (e.key === "Enter") btnGo?.click(); });
}

// Play/pause
btnPlay.addEventListener("click", () => {
  if (isPlaying) { player.pause(); setPlayState(false); } else { _startStream(); }
});

player.addEventListener("play",    () => setPlayState(true));
player.addEventListener("playing", () => setPlayState(true));
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
    btnRecord.setAttribute("aria-label",   "Start recording");
    // Stop elapsed timer
    clearInterval(_recTimer);
    _recTimer = null;
    _recStart = null;
    if (recElapsed) recElapsed.textContent = "";
    loadRecordings();
    showToast("Recording saved");
  } else {
    const res = await api("POST", "/record/start");
    if (!res.error) {
      isRecording = true;
      btnRecord.classList.add("active");
      btnRecord.setAttribute("aria-pressed", "true");
      btnRecord.setAttribute("aria-label",   "Stop recording");
      // Start elapsed timer — uses started_at from response if available, else Date.now()
      _recStart = res.started_at ? res.started_at * 1000 : Date.now();
      if (recElapsed) {
        _recTimer = setInterval(() => {
          const s = Math.floor((Date.now() - _recStart) / 1000);
          recElapsed.textContent = formatDuration(s);
        }, 1000);
      }
      showToast("Recording started");
    }
  }
});

// Save preset
btnSavePreset.addEventListener("click", () => {
  presetNameInput.value = elStationName.textContent !== "Squelch" ? elStationName.textContent : "";
  openModal();
});
btnPresetCancel.addEventListener("click", closeModal);
btnPresetSave.addEventListener("click", async () => {
  const name = presetNameInput.value.trim();
  if (name) { await saveCurrentPreset(name); closeModal(); showToast("Preset saved"); }
});
presetNameInput.addEventListener("keydown", e => { if (e.key === "Enter") btnPresetSave.click(); });
modalPreset.addEventListener("click", e => { if (e.target === modalPreset) closeModal(); });

// Bottom nav
document.querySelectorAll(".nav-tab").forEach(t => {
  t.addEventListener("click", () => activateTab(t.dataset.panel));
});

// Desktop diagnostics toggle
document.getElementById("btn-diag-toggle")?.addEventListener("click", function () {
  const drawer = document.getElementById("diagnostics-drawer");
  if (!drawer) return;
  drawer.open = !drawer.open;
  saveLayoutPref("diagOpen", drawer.open);
  this.setAttribute("aria-expanded", String(drawer.open));
  if (drawer.open) drawer.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

// Persist diagnostics state on native toggle
document.getElementById("diagnostics-drawer")?.addEventListener("toggle", function () {
  saveLayoutPref("diagOpen", this.open);
  const btn = document.getElementById("btn-diag-toggle");
  if (btn) btn.setAttribute("aria-expanded", String(this.open));
});

// Column collapse buttons (desktop ≥1024px; also tablet for #right-col)
document.querySelectorAll(".col-collapse-btn").forEach(btn => {
  btn.addEventListener("click", () => toggleColCollapse(btn.dataset.col));
});

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
  const tag = document.activeElement?.tagName?.toLowerCase();
  const inInput = tag === "input" || tag === "textarea" || document.activeElement?.isContentEditable;
  if (inInput) return;

  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault();
    const r = BAND_RANGES[currentBand];
    const dir = e.key === "ArrowLeft" ? -1 : 1;
    const newFreq = clamp(parseFloat((currentFreq + dir * r.step).toFixed(4)), r.min, r.max);
    setFreq(newFreq);
    tune(newFreq);
  }

  if (e.key === " ") {
    e.preventDefault();
    if (isPlaying) { player.pause(); setPlayState(false); } else { _startStream(); }
  }

  if (e.key === "r" || e.key === "R") {
    e.preventDefault();
    btnRecord.click();
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Init
// ─────────────────────────────────────────────────────────────────────────────

setBand("fm");
initPanels();
initLibTabs();
initColLayout();
setupColDrag();
setupMiniPlayer();
setupFreqStrip();
connectWs();
loadPresets();
loadRecordings();
loadHistory();
loadLayoutPrefs();

// Resume recording timer if already recording on load
api("GET", "/record/status").then(s => {
  if (s.recording) {
    isRecording = true;
    btnRecord.classList.add("active");
    btnRecord.setAttribute("aria-pressed", "true");
    btnRecord.setAttribute("aria-label",   "Stop recording");
    _recStart = s.started_at ? s.started_at * 1000 : Date.now();
    if (recElapsed) {
      _recTimer = setInterval(() => {
        const secs = Math.floor((Date.now() - _recStart) / 1000);
        recElapsed.textContent = formatDuration(secs);
      }, 1000);
    }
  }
});

// Redraw freq strip on window resize; restore column layout when leaving mobile.
let _stripResizeTimer = null;
let _prevWasPhone   = window.innerWidth < 640;
let _prevWasDesktop = window.innerWidth >= 1024;

window.addEventListener("resize", () => {
  const isPhone   = window.innerWidth < 640;
  const isDesktop = window.innerWidth >= 1024;

  // Crossing phone → tablet/desktop: clear inline display styles set by the
  // mobile panel switcher so CSS media queries take control again.
  if (_prevWasPhone && !isPhone) {
    ["left-col", "center-col", "right-col"].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.style.display = ""; el.style.opacity = ""; el.style.animation = ""; }
    });
  }

  // Crossing tablet → desktop: re-apply saved column order and collapse state.
  if (!_prevWasDesktop && isDesktop) initColLayout();

  _prevWasPhone   = isPhone;
  _prevWasDesktop = isDesktop;
  clearTimeout(_stripResizeTimer);
  _stripResizeTimer = setTimeout(drawFreqStrip, 150);
});
