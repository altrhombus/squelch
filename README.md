# Squelch

A self-hosted SDR radio streamer for the Raspberry Pi. Tune FM (stereo), AM, HD Radio, and scanner frequencies — stream to any device on your home network via a mobile-friendly web interface.

- **AAC-LC audio stream** — chunked HTTP delivery, natively decoded by iOS/macOS/Chrome with no plugins or apps required
- **FM stereo** — custom DSP pipeline (numpy/scipy): pilot demodulation, Wiener filter noise reduction, stereo blend, de-emphasis, K-weighted AGC
- **RDS metadata** — station name, artist/title, program type (FM)
- **HD Radio** — full NRSC-5 decode including cover art and rich metadata
- **AirPlay** — tap the AirPlay button in Safari to route audio to any AirPlay target
- **Recording** — one-tap recording, auto-named from station metadata
- **Web UI** — no SDR jargon exposed; just stations, a dial, and a play button

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
git clone https://github.com/YOUR_USERNAME/squelch.git
cd squelch

# 2. Run the install script (builds librtlsdr, installs Python deps, sets up systemd service)
bash services/install.sh

# 3. Edit your config
nano config/settings.yaml

# 4. Start the service
sudo systemctl start squelch

# 5. Open in browser (replace with your Pi's IP or hostname)
open http://squelch.local:8000
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
| `GET` | `/history` | Recently played (last 100) |
| `DELETE` | `/history/{id}` | Delete a history entry |
| `DELETE` | `/history` | Clear all history |
| `POST` | `/squelch` | Set squelch threshold (0–100) |

### iOS / iPadOS integration

The stream URL (`/stream`) works directly with `AVPlayer` — the AAC-LC ADTS format is hardware-decoded on every Apple device. In Safari, the native AirPlay button on the `<audio>` element lets you route audio to any AirPlay target without any app. For a native app, `GET /stream/url` returns the stream URL and the WebSocket at `/ws` provides real-time metadata.

---

## Supported bands

| Band | Notes |
|---|---|
| FM broadcast (87.5–108 MHz) | Full stereo, RDS metadata, noise-adaptive DSP |
| AM broadcast (530–1700 kHz) | Requires RTL-SDR v3/v4 direct sampling; long-wire antenna recommended |
| HD Radio (87.5–108 MHz) | Requires `nrsc5`; ~400% CPU on Pi 4; falls back to analog FM if unavailable |
| Scanner / NFM (25–1300 MHz) | Narrowband FM; aviation band (118–137 MHz) auto-switches to AM demodulation |

---

## FM signal processing

All FM DSP runs in Python (numpy/scipy) — no GNU Radio required. The pipeline from IQ samples to encoded audio:

1. **Decimation** — 1.2 MHz IQ → 240 kHz via polyphase resampling
2. **FM discriminator** — phase-difference demodulator → composite baseband
3. **Click blanking** — two-stage: hard clip at ±1.5, then burst interpolation for multi-sample phase slips
4. **Stereo decoding** — pilot BPF (17–21 kHz), Hilbert carrier doubling to 38 kHz, coherent DSB-SC L-R demodulation
5. **Stereo blend** — continuous fade toward mono as pilot SNR or discriminator SNR drops; prevents the stereo hiss that would otherwise appear on weak stations
6. **Software gain control** — adaptively steps RTL-SDR hardware gain every ~5 s to stay in the optimal SNR operating range without ADC saturation
7. **Wiener filter noise reduction** — Ephraim-Malah (1984) decision-directed Wiener gain across the full 0–15 kHz band, operating on the pre-emphasized signal where HF SNR is highest (+10–17 dB vs. post-de-emphasis). MinStat circular-buffer noise floor estimation keeps the filter calibrated even on stations with no silence gaps.
8. **De-emphasis** — 75 µs (US) or 50 µs (EU) first-order IIR
9. **Stereo width restoration** — selectively recovers midrange (300–3500 Hz) L-R content on weak stations where the stereo blend has suppressed it
10. **K-weighted AGC** — loudness normalization per ITU-R BS.1770-4; treats spectrally bright and bass-heavy stations as equally loud
11. **Soft-knee limiter** — tanh rolloff above 0.85 normalised amplitude; avoids the HF harmonics a hard clip would introduce

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
| `_WIENER_FLOOR` | `0.24` | Minimum per-bin Wiener gain (−12 dB floor). Prevents inter-formant bins from collapsing to near-zero, which causes a "watery" voice quality on weak stations. | Raise toward `0.30` if voices still sound watery (less noise reduction, less modulation). Lower toward `0.10` for more aggressive noise removal on moderate stations. |

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
| `_GAIN_HOLD_BLOCKS` | `50` | Minimum blocks (~5.5 s) between gain steps. | Raise if gain is hunting (stepping up and down frequently). |

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

---

## Stretch goals / roadmap

- [ ] AirPlay push from Pi (`owntone`)
- [ ] Home Assistant MQTT integration
- [ ] Native iOS/iPadOS app (SwiftUI + AVPlayer)
- [ ] Scheduled recordings
- [ ] Spectrum waterfall display

---

## Troubleshooting

**`extra callback data lost` in logs / audio dropouts**: The DSP thread is taking longer than the ~109 ms block interval. Likely causes: Pi is thermally throttling (check `vcgencmd measure_temp`), or an inadequate USB power supply. Adding a heatsink and ensuring a quality USB-C supply (≥ 3 A) usually resolves this.

**`rtl_sdr: error -3 (no device found)`**: The kernel DVB driver is loaded and has claimed the device. Run:
```bash
sudo modprobe -r dvb_usb_rtl28xxu
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/rtl-blocklist.conf
```

**No audio after tuning**: Check `journalctl -u squelch -f`. The most common cause is a missing or incorrect `ppm_correction` value in `settings.yaml` shifting the station off-frequency. Run `rtl_test -p` for several minutes to get your dongle's PPM offset.

**Poor AM reception**: AM direct sampling requires an RTL-SDR v3 or v4. A 10–20 m wire connected to the antenna input makes a large difference; indoors reception is often marginal without one.

**HD Radio not decoding**: Verify `nrsc5` is on `$PATH` (`which nrsc5`). HD Radio requires ~400% CPU on a Pi 4 — if the Pi is also running other loads, the decoder may fall behind. Check temperature and CPU governor (`cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor`; set to `performance` for best results).