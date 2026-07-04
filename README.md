# Squelch

[![CI](https://github.com/altrhombus/squelch/actions/workflows/ci.yml/badge.svg)](https://github.com/altrhombus/squelch/actions/workflows/ci.yml)

A self-hosted SDR radio streamer for the Raspberry Pi. Tune FM (stereo), AM, HD Radio, weather radio, and scanner frequencies — stream to any device on your home network via a modern, responsive web interface.

- **AAC-LC audio stream** — chunked HTTP delivery, natively decoded by iOS/macOS/Chrome with no plugins or apps required
- **FM stereo** — fully stateful custom DSP pipeline (numpy/scipy): phase-continuous carrier recovery, Ephraim-Malah Wiener noise reduction, adaptive stereo blend + width restoration, de-emphasis, K-weighted (BS.1770) AGC, soft-knee limiting — all block-boundary-seamless
- **A serious RDS decoder** — coherent pilot-locked carrier, adaptive symbol-timing recovery (tolerates >200 ppm dongle clocks), position-tracked block sync that survives bit errors, burst error correction with a double-confirmation guard against mis-corrections, RT+ structured tags, tiered RadioText emission, and successor-graph reassembly of song info from stations that page it through the PS field
- **HD detection** — spots IBOC digital sidebands while you listen to analog FM and offers a one-tap switch to HD
- **HD Radio** — full NRSC-5 decode including cover art and rich metadata
- **Self-calibrating tuning** — measures your dongle's crystal error live (from the FM stereo pilot or a NOAA carrier) and auto-recenters narrowband channels (AFC), so WX and scanner work even on uncalibrated dongles
- **Noise-reduced NFM** — weather radio and scanner voice get the same Wiener noise reduction as FM, plus a squelch you control from the UI
- **Runs cool** — the SDR powers down entirely (tuner off, USB idle) whenever nobody is listening, and wakes in about a second on the next connection
- **Seek scan** — hold a tuning chevron and Squelch steps the band and stops on the next receivable station, like a car radio
- **AirPlay** — tap the AirPlay button in Safari to route audio to any AirPlay target
- **Recording** — one-tap recording, auto-named from station metadata; scheduled recordings via the API
- **Icecast output** (optional) — push the stream to an Icecast mount with live now-playing metadata for VLC/Sonos-style clients
- **Web UI in the Golden Gate design language** — one continuous radio surface with a scrub-able glass frequency ruler (your presets marked on the tape), a flush library sidebar on desktop, floating tab bar on iPhone/iPad, art-tinted dynamic accents, and an adjustable glass intensity setting; no SDR jargon anywhere

---

## Hardware requirements

| Component | Notes |
|---|---|
| RTL-SDR dongle (RTL2832U) | RTL-SDR Blog v3 or v4 recommended; v4 has a better noise figure |
| Raspberry Pi 4 or 5 | Pi 3B+ works; Pi Zero 2 W is too slow for FM stereo DSP |
| Antenna | See below |

### Antenna tips

Software cannot overcome a weak signal, but a simple antenna upgrade makes a dramatic difference:

- **FM broadcast**: A 75 cm wire (1/4-wave dipole) dramatically outperforms the bundled stub. Suspend it vertically for best results.
- **AM broadcast**: AM requires direct sampling (RTL-SDR v3/v4). A long-wire antenna (10–20 m) helps considerably.
- **EMI**: Place the RTL-SDR on a USB extension cable, away from the Pi's CPU heat and switching noise.

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/altrhombus/squelch.git
cd squelch

# 2. Run the install script (builds librtlsdr, installs Python deps, sets up systemd service)
bash services/install.sh

# 3. Edit your config
nano config/settings.yaml

# 4. Start the service
sudo systemctl start squelch

# 5. Open http://squelch.local:8000 in a browser
#    (replace with your Pi's IP or hostname)
```

### Manual (without the install script)

```bash
# Build librtlsdr from the RTL-SDR Blog fork (the distro package is missing
# symbols required by pyrtlsdr and has worse driver support)
sudo apt-get remove -y rtl-sdr librtlsdr-dev librtlsdr0 2>/dev/null || true
sudo apt-get install -y libusb-1.0-0-dev cmake build-essential git python3 python3-venv python3-numpy python3-scipy
git clone --depth 1 https://github.com/rtlsdrblog/rtl-sdr-blog
cmake -S rtl-sdr-blog -B rtl-sdr-blog/build -DDETACH_KERNEL_DRIVER=ON
make -C rtl-sdr-blog/build -j$(nproc) && sudo make -C rtl-sdr-blog/build install && sudo ldconfig

# Block the conflicting kernel DVB driver
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/rtl-blocklist.conf
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true

# Optional: HD Radio (not in apt — build from source)
sudo apt-get install -y cmake libfftw3-dev librtlsdr-dev build-essential
git clone --depth 1 https://github.com/theori-io/nrsc5
cmake -S nrsc5 -B nrsc5/build -DUSE_RTLSDR=ON && make -C nrsc5/build -j$(nproc)
sudo make -C nrsc5/build install && sudo ldconfig

# --system-site-packages lets the venv use apt-installed numpy/scipy,
# which are optimised for the Pi (NEON SIMD via OpenBLAS)
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
cp config/settings.yaml.example config/settings.yaml

# Run in dev mode
.venv/bin/python -m backend.main
```

---

## Configuration

Edit `config/settings.yaml` (copied from `settings.yaml.example` by the install script):

```yaml
server:
  host: "0.0.0.0"
  port: 8000

sdr:
  device_index: 0        # RTL-SDR device index (0 = first dongle)
  gain: "auto"           # or a number like 30 for manual gain in dB
  ppm_correction: 0      # run `rtl_test -p` to find your dongle's PPM offset
  deemphasis_us: 75      # 75 for USA/Canada, 50 for Europe/Australia/Japan

default_presets:
  - name: "WBEZ 91.1"
    frequency: 91.1
    band: fm
```

### Icecast output (optional)

Squelch can push the same AAC stream to an Icecast2 mount, including live now-playing metadata from RDS/HD Radio (so players like VLC show artist/title). Install and start Icecast (`sudo apt-get install icecast2` — a starter config is provided at `config/icecast.xml.example`), then enable it in `settings.yaml`:

```yaml
icecast:
  enabled: true
  host: "localhost"
  port: 8001
  source_password: "changeme"   # must match <source-password> in icecast.xml
  mount: "/radio"
  keep_alive: false
```

With `keep_alive: false` (the default) the mount is only live while someone is listening or recording, so the Pi's DSP idle-suspend keeps working. Set it to `true` to keep the mount up whenever a station is tuned — Icecast clients can then connect at any time, at the cost of the DSP running continuously.

---

## API

The backend exposes a REST API (full docs at `http://your-pi:8000/docs`):

| Method | Path | Description |
|---|---|---|
| `GET` | `/stream` | Live AAC-LC audio stream (`audio/aac`, chunked) |
| `GET` | `/stream/url` | Returns the stream URL for use with AVPlayer etc. |
| `POST` | `/tune` | `{"frequency": 91.1, "band": "fm"}` |
| `GET` | `/status` | Current station, metadata, signal strength |
| `WS` | `/ws` | Live metadata + signal updates |
| `GET` | `/presets` | List presets |
| `POST` | `/presets` | Create preset |
| `PATCH` | `/presets/{id}` | Update preset |
| `DELETE` | `/presets/{id}` | Delete preset |
| `POST` | `/record/start` | Start recording |
| `POST` | `/record/stop` | Stop recording |
| `GET` | `/record/status` | Recording state and filename |
| `GET` | `/recordings` | List recordings |
| `DELETE` | `/recordings/{id}` | Delete a recording |
| `GET` | `/recordings/{id}/download` | Download recording |
| `GET` | `/schedules` | List scheduled recordings |
| `POST` | `/schedules` | Create a scheduled recording (see below) |
| `DELETE` | `/schedules/{id}` | Delete a scheduled recording |
| `GET` | `/history` | Recently played (last 100) |
| `DELETE` | `/history/{id}` | Delete a history entry |
| `DELETE` | `/history` | Clear all history |
| `POST` | `/squelch` | Set squelch threshold (0–100) |

### Scheduled recordings

Recordings can run automatically on a cron schedule (API only — no UI yet):

```bash
curl -X POST http://squelch.local:8000/schedules -H 'Content-Type: application/json' -d '{
  "name": "Morning Show",
  "frequency": 91.1,
  "band": "fm",
  "duration_seconds": 3600,
  "cron_expr": "0 9 * * 1-5"
}'
```

At each trigger, Squelch tunes to the station, records for the duration, then restores whatever was previously tuned. `frequency` uses the same units as presets (MHz; kHz for AM), and `cron_expr` is a standard 5-field crontab expression.

### iOS / iPadOS integration

The stream URL (`/stream`) works directly with `AVPlayer` — the AAC-LC ADTS format is hardware-decoded on every Apple device. In Safari, the native AirPlay button on the `<audio>` element lets you route audio to any AirPlay target without any app. For a native app, `GET /stream/url` returns the stream URL and the WebSocket at `/ws` provides real-time metadata.

---

## Supported bands

| Band | Notes |
|---|---|
| FM broadcast (87.5–108 MHz) | Full stereo, RDS metadata, noise-adaptive DSP |
| AM broadcast (530–1700 kHz) | Requires RTL-SDR v3/v4 direct sampling; long-wire antenna recommended |
| HD Radio (87.5–108 MHz) | Requires `nrsc5`; ~400% CPU on Pi 4; falls back to analog FM if unavailable |
| Weather radio (162.4–162.55 MHz) | All seven NOAA channels as one-tap chips; NFM with noise reduction, AFC, squelch |
| Scanner / NFM (25–1300 MHz) | Keypad entry, squelch, AFC; aviation band (118–137 MHz) auto-switches to AM demodulation |

---

## FM signal processing

All FM DSP runs in Python (numpy/scipy) — no GNU Radio required. Every stage is **stateful across processing blocks** (carried filter states, resampler history, mixer phase, decimation phase), so consecutive ~218 ms blocks behave exactly like one continuous run — no block-rate edge artifacts anywhere in the chain. The pipeline from IQ samples to encoded audio:

1. **Decimation** — 1.2 MHz IQ → 240 kHz via a stateful overlap-save polyphase resampler (bit-identical to resampling the whole signal at once)
2. **FM discriminator** — phase-difference demodulator with sample carry across blocks → composite baseband
3. **Click blanking** — two-stage: hard clip at ±1.5, then burst interpolation for multi-sample phase slips
4. **Stereo decoding** — analytic 19 kHz pilot via heterodyne + stateful lowpass (phase-continuous, no FFTs in the hot path), squared to a 38 kHz carrier for coherent DSB-SC L-R demodulation
5. **Stereo blend** — continuous fade toward mono as pilot SNR or discriminator SNR drops; prevents the stereo hiss that would otherwise appear on weak stations
6. **Software gain control** — adaptively steps RTL-SDR hardware gain every ~5 s to stay in the optimal SNR operating range without ADC saturation
7. **Wiener filter noise reduction** — Ephraim-Malah (1984) decision-directed Wiener gain across the full 0–15 kHz band, operating on the pre-emphasized signal where HF SNR is highest (+10–17 dB vs. post-de-emphasis), floored by a physics-based noise estimate from the 65–90 kHz discriminator band
8. **De-emphasis** — 75 µs (US) or 50 µs (EU) first-order IIR
9. **Stereo width restoration** — selectively recovers midrange (300–3500 Hz) L-R content on weak stations where the stereo blend has suppressed it
10. **K-weighted AGC** — loudness normalization per ITU-R BS.1770-4; treats spectrally bright and bass-heavy stations as equally loud
11. **Soft-knee limiter** — tanh rolloff above 0.85 normalised amplitude; avoids the HF harmonics a hard clip would introduce

## RDS decoding

The RDS decoder (`backend/sdr/rds.py`) is built for weak, real-world signals:

- **Coherent carrier** — the 57 kHz subcarrier is regenerated by cubing the analytic pilot, so it tracks the station exactly
- **Adaptive symbol timing** — a per-phase energy tracker acquires the sampling point from an arbitrary start and slews with clock drift; biphase-lobe-aware so it never hunts between the two half-symbol peaks. Tolerates >200 ppm uncorrected dongle crystals
- **Position-tracked block sync** — a CRC-failed block blanks its slot instead of costing re-acquisition; sync survives isolated bit errors and only re-acquires on a genuine fade
- **Burst error correction** — the (26,16) code's syndrome table corrects ≤2-bit bursts, roughly doubling group throughput at threshold SNR. A double-reception confirmation gate keeps rare mis-corrections (a >2-bit error "corrected" into a different valid word) off the display
- **Rich payloads** — PS, RadioText with tiered partial→complete emission, RT+ structured artist/title tags (IEC 62106 Annex A), PTY, PI, extended character set (EN 50067 Annex E)
- **Dynamic-PS reassembly** — stations that page song info through the 8-character PS field are reconstructed via a successor graph that tolerates heavy page loss, with rotation scoring to find the true message start
- Everything corruption-prone is **debounced or double-confirmed** (PI, PTY, A/B flag flips, extended characters, corrected groups), and provisional text is display-only — it never reaches history or the cover-art lookup until confident

## NFM, weather radio, and self-calibration

WX and scanner channels decode as narrowband FM with a proper channel filter, the same Wiener noise reduction as FM (driven by an out-of-band discriminator noise measurement), and a silence-gated AGC. Two calibration aids make cheap dongles behave:

- **`diag.pilot_offset_hz`** (FM) and **`diag.carrier_offset_hz`** (WX) measure your crystal's ppm error live — the FM pilot is transmitter-exact to ±2 Hz and NOAA carriers are exact, so you can calibrate `ppm_correction` from the diagnostics popover without ever running `rtl_test`
- **AFC** — on WX/scanner, the pipeline measures the carrier offset (power centroid, immune to modulation) and recenters the tuner automatically when it's stable and >1 kHz off, so narrowband reception works even with `ppm_correction: 0`

---

## DSP tuning reference

All constants below are in `backend/sdr/fm.py` (module-level) or `backend/sdr/pipeline.py` unless noted. Changes take effect on service restart.

### Wiener filter noise reduction

| Constant | Default | Effect | When to adjust |
|---|---|---|---|
| `_MINSTAT_BIAS` | `1.66` | Scales the MinStat minimum before it becomes the noise floor estimate. Higher → more aggressive subtraction. | Raise toward `2.0` if residual hiss is still audible on weak stations. Lower toward `1.25` if musical-noise artefacts appear on strong stations. |
| `_PHYS_SCALE` | `200.0` | Converts discriminator noise RMS² to per-STFT-bin power for the physics floor. Analytical value is 307; 200 is conservative. | Raise toward `250` if the floor seems too weak on very noisy stations. Lower toward `150` if over-subtraction artefacts appear. |
| `_MINSTAT_FRAMES` | `128` | Length of MinStat circular buffer (128 × 10.7 ms ≈ 1.4 s of history). | Don't raise above `256` — the axis=0 min scan cost was the source of USB callback overflows at 256. Lower reduces memory but makes the estimate noisier. |
| `alpha_dd` | `0.92` | Decision-directed Wiener smoother time constant (τ ≈ 250 ms). Lower = faster gain response. | Lower toward `0.88` if the Wiener sounds sluggish on transients. Don't go below `0.88` — sibilant offset artefacts appear (tested). |
| `_WIENER_FLOOR` | `0.24` | Weak-signal minimum per-bin Wiener gain (−12 dB). The floor adapts with signal quality: it slides from this value at blend=0 down to `_WIENER_FLOOR_STRONG` (`0.10`, −20 dB) at blend=1. Prevents inter-formant bins from collapsing to near-zero, which causes a "watery" voice quality on weak stations. | Raise toward `0.30` if voices still sound watery (less noise reduction, less modulation). Lower `_WIENER_FLOOR_STRONG` for more residual-noise suppression on strong stations. |

### Stereo blend

| Constant | Default | Effect | When to adjust |
|---|---|---|---|
| `_NOISE_RATIO_SCALE` | `1.5` | noise_rms / pilot_rms ratio at which the noise gate fully closes (blend → 0). | Raise if strong stations are blending to mono too eagerly. Lower if weak stations have audible stereo hiss. |
| `_STEREO_RESTORE_MAX` | `1.2` | Maximum midrange (300–3500 Hz) L-R boost applied when blend is low. | Lower toward `0.8` if the stereo width restoration sounds unnatural on marginal signals. |
| `deemphasis_us` | `75` | De-emphasis time constant. **75 µs for USA/Canada, 50 µs for Europe/Australia/Japan.** Set in `config/settings.yaml`. | Must match your broadcast region or the audio will sound too bright (wrong value) or dull. |

### AGC

| Constant / value | Default | Effect | When to adjust |
|---|---|---|---|
| `target_gain = 0.12 / rms_k_smooth` | target RMS ≈ 0.12 | K-weighted output level target. Hardcoded in `FmStereoDemodulator.process()`. | Edit the `0.12` literal if output is consistently too quiet or too loud across all stations. |
| `_LIMITER_KNEE` | `0.85` | Amplitude above which the soft-knee limiter engages. | Raise toward `0.95` for more headroom before limiting. Lower toward `0.75` for a more compressed sound. |

### Software gain control (RTL-SDR hardware gain)

These are in `backend/sdr/pipeline.py`.

| Constant | Default | Effect | When to adjust |
|---|---|---|---|
| `_FM_GAIN_START` | `30.0 dB` | Starting gain after tune. | Lower to `20–25 dB` if you have an external LNA that already provides sufficient gain. |
| `_IQ_RMS_LO` | `0.07` | IQ RMS floor — gain steps up when below this. | Raise slightly if the controller keeps stepping up gain unnecessarily on a marginal signal. |
| `_IQ_RMS_HI` | `0.38` | IQ RMS ceiling — gain steps down when above this (ADC saturation risk). | Lower if you hear clipping on very strong local stations. |
| `_NOISE_RATIO_MAX` | `2.0` | noise_rms / pilot_rms above which the controller steps gain down for SNR quality. | Lower toward `1.5` if the controller doesn't back off gain on noisy marginal stations. |
| `_GAIN_HOLD_BLOCKS` | `25` | Minimum blocks (~5 s at 218 ms/block) between gain steps. | Raise if gain is hunting (stepping up and down frequently). |

---

## Tech stack

| Layer | What |
|---|---|
| Backend | FastAPI + uvicorn |
| SDR I/O | pyrtlsdr (async stream) |
| DSP | numpy, scipy |
| Audio encoding | PyAV (AAC-LC, 128 kbps stereo / 48 kbps mono, ADTS) |
| Database | SQLite via aiosqlite (presets, history, recordings) |
| Frontend | Vanilla JS + CSS (no build step) |

## Architecture

The SDR device is fully closed (tuner powered off, no USB transfers) whenever no listener, recorder, or keep-alive Icecast mount needs audio, and reopens on the next connection — the dongle is usually the hottest part of the build, so this matters more for enclosure temperature than any software optimisation.

```
RTL-SDR ──USB──▶ pyrtlsdr async stream (1.2 MHz IQ, ~218 ms blocks)
                     │
                     ▼  DSP thread (numpy/scipy)
       FM stereo / AM / NFM demodulation
       Wiener NR · stereo blend · de-emphasis · AGC · limiter
                     │                    │
          composite tap (240 kHz)         ▼
                     │            AAC-LC encoder (PyAV, ADTS)
                     ▼                    │
          RDS decoder thread              ▼
                     │           StreamingManager (per-client queues)
                     ▼               ├──▶ GET /stream   (browsers, AVPlayer, AirPlay)
              MetadataState          ├──▶ Recorder      (.aac files + SQLite)
                     │               └──▶ IcecastPusher (optional mount)
                     ▼
              WebSocket /ws  (metadata, signal, diagnostics)

HD Radio: nrsc5 subprocess ──PCM pipe──▶ resample 44.1→48 kHz ──▶ same encoder path
```

---

## Development

You don't need SDR hardware to hack on Squelch — the test suite drives the full FM demodulator, the RDS decoder (down to a synthesized composite waveform with a biphase-modulated 57 kHz subcarrier, clock drift, and noise), and the AM/NFM paths entirely with synthetic IQ:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

When something *sounds* wrong on air, record it in the UI and run the recording through the forensics tool — it measures the artifacts we've learned to chase (AGC onset blasts, Wiener sibilance warble, clipping, spectral balance, stereo width):

```bash
python tools/waveform_review.py ~/recordings/that-weird-noise.aac
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup details, code style, and how DSP changes are validated.

---

## Stretch goals / roadmap

- [ ] AirPlay push from Pi (`owntone`)
- [ ] Home Assistant MQTT integration
- [ ] Native iOS/iPadOS app (SwiftUI + AVPlayer)
- [x] Scheduled recordings (REST API; no UI yet)
- [ ] Spectrum waterfall display

---

## Security

Squelch is designed for a **trusted home LAN** and has **no authentication** — anyone who can reach the port can tune the radio, start/stop recordings, and delete presets, recordings, and history.

- **Do not port-forward Squelch to the internet.** For remote access, use a VPN (WireGuard, Tailscale) or put it behind a reverse proxy that adds authentication (e.g. Caddy/nginx with basic auth — note the `<audio>` stream and WebSocket must pass through too).
- The server binds to `0.0.0.0` by default so other devices on your network can reach it; set `server.host` in `settings.yaml` to restrict this.
- Cover-art lookup sends the currently playing artist/title to Apple's iTunes Search API. Set `metadata.itunes_lookup: false` in `settings.yaml` to disable it.

---

## License

Copyright © 2026 altrhombus

Squelch is free software, licensed under the **GNU General Public License v3.0 or later** ([LICENSE](LICENSE)). You can redistribute it and/or modify it under the terms of the GPL as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY — see the license text for details.

GPLv3 is required for license compatibility with [pyrtlsdr](https://github.com/pyrtlsdr/pyrtlsdr) (GPLv3), which Squelch uses as a library. HD Radio decoding uses [nrsc5](https://github.com/theori-io/nrsc5) (GPLv3) as a separate subprocess.

---

## Troubleshooting

**`extra callback data lost` in logs / audio dropouts**: The DSP thread is taking longer than the ~218 ms block interval. Likely causes: Pi is thermally throttling (check `vcgencmd measure_temp`), or an inadequate USB power supply. Adding a heatsink and ensuring a quality USB-C supply (≥ 3 A) usually resolves this.

**`rtl_sdr: error -3 (no device found)`**: The kernel DVB driver is loaded and has claimed the device. Run:
```bash
sudo modprobe -r dvb_usb_rtl28xxu
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/rtl-blocklist.conf
```

**No audio after tuning**: Check `journalctl -u squelch -f`. Wideband FM is very tolerant of crystal error, but weather/scanner channels are not — if WX is static, your dongle likely needs `ppm_correction`. You don't need `rtl_test`: tune WX2 (162.400) and read `carrier_offset_hz` from the signal-details popover — your ppm is `−offset / 162.4`. (AFC will usually rescue reception even uncorrected, but a corrected dongle locks in faster.) Cross-check on a strong FM station via `pilot_offset_hz` (ppm ≈ −offset / 0.019).

**Poor AM reception**: AM direct sampling requires an RTL-SDR v3 or v4. A 10–20 m wire connected to the antenna input makes a large difference; indoors reception is often marginal without one.

**HD Radio not decoding**: Verify `nrsc5` is on `$PATH` (`which nrsc5`). HD Radio requires ~400% CPU on a Pi 4 — if the Pi is also running other loads, the decoder may fall behind. Check temperature and CPU governor (`cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor`; set to `performance` for best results).