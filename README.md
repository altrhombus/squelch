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