# Squelch

A self-hosted SDR radio streamer for the Raspberry Pi. Tune FM (stereo), AM, HD Radio, and scanner frequencies — stream to any device on your home network via a mobile-friendly web interface.

- **HLS streaming** — battery-optimized for iPhone (hardware AAC decode, chunked downloads)
- **FM stereo** via GNU Radio — proper L/R pilot decoding, not just mono
- **RDS metadata** — station name, artist/title, program type (FM)
- **HD Radio** — full NRSC-5 decode including cover art and rich metadata
- **AirPlay** — tap the AirPlay button in Safari to route audio to any AirPlay target
- **Recording** — one-tap recording, auto-named from station metadata
- **Web UI** — no SDR jargon exposed; just stations, a dial, and a play button

![Squelch UI screenshot](docs/screenshot.png)

---

## Hardware requirements

| Component | Notes |
|---|---|
| RTL-SDR dongle (RTL2832U) | RTL-SDR Blog v3 or v4 recommended; v4 has a better noise figure |
| Raspberry Pi 4 or 5 | Pi 3B+ works; Pi Zero 2 W is too slow for GNU Radio FM stereo |
| Antenna | See below |

### Antenna tips

Software cannot overcome a weak signal, but a simple antenna upgrade makes a dramatic difference:

- **FM broadcast**: A 75cm wire (1/4-wave dipole) dramatically outperforms the bundled antenna. Suspend it vertically for best results.
- **AM broadcast**: AM requires either direct sampling (RTL-SDR v3/v4 with `-D 2`) or an upconverter. A long wire antenna (10–20m) helps considerably.
- **EMI**: Place the RTL-SDR on a USB extension cable, away from the Pi's CPU heat and noise.

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/squelch.git
cd squelch

# 2. Run the install script (installs system packages, Python venv, systemd service)
bash services/install.sh

# 3. Edit your config
nano config/settings.yaml

# 4. Start the service
sudo systemctl start squelch

# 5. Open in browser (replace with your Pi's IP)
open http://squelch.local:8000
```

### Manual (without the install script)

```bash
sudo apt-get install rtl-sdr gnuradio gr-osmosdr gr-rds ffmpeg python3-venv

# Optional: HD Radio (not in apt — build from source)
sudo apt-get install cmake libfftw3-dev librtlsdr-dev build-essential
git clone --depth 1 https://github.com/theori-io/nrsc5
cmake -S nrsc5 -B nrsc5/build -DUSE_RTLSDR=ON && make -C nrsc5/build -j$(nproc)
sudo make -C nrsc5/build install && sudo ldconfig

# Block the conflicting kernel DVB driver
# ('blacklist' is the Linux kernel modprobe directive name)
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/rtl-blocklist.conf

python3 -m venv .venv
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
  gain: "auto"           # or a number like 30 for manual gain
  ppm_correction: 0      # run `rtl_test -p` to find your dongle's PPM offset
  deemphasis_us: 75      # 75 for USA, 50 for Europe/Australia

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
| `POST` | `/tune` | `{"frequency": 91.1, "band": "fm"}` |
| `GET` | `/status` | Current station, metadata, signal |
| `GET` | `/stream/url` | HLS stream URL |
| `WS` | `/ws` | Live metadata + signal updates |
| `GET` | `/presets` | List presets |
| `POST` | `/presets` | Create preset |
| `DELETE` | `/presets/{id}` | Delete preset |
| `POST` | `/record/start` | Start recording |
| `POST` | `/record/stop` | Stop recording |
| `GET` | `/recordings` | List recordings |
| `GET` | `/recordings/{id}/download` | Download recording |
| `GET` | `/history` | Recently played (last 100) |

### iOS / iPadOS app

The API is designed to be iOS-app-ready. The HLS stream URL works directly with `AVPlayer`, and the WebSocket at `/ws` provides real-time metadata. See `GET /stream/url` for the stream URL.

---

## Supported bands

| Band | Notes |
|---|---|
| FM broadcast (87.5–108 MHz) | Stereo via GNU Radio `wbfm_receive`; RDS via `gr-rds` |
| AM broadcast (530–1700 kHz) | Requires RTL-SDR v3/v4 (direct sampling); long-wire antenna recommended |
| HD Radio (87.5–108 MHz) | Requires `nrsc5`; ~400% CPU on Pi 4; falls back to analog FM if unavailable |
| Scanner / NFM (25–1300 MHz) | Narrowband FM; aviation band (118–137 MHz) auto-switches to AM mode |

---

## DSP features

- **Stereo auto-blend**: Fades toward mono as pilot SNR drops — eliminates stereo noise on weak stations
- **De-emphasis**: Correct 75µs (US) / 50µs (EU) rolloff applied in GNU Radio
- **Squelch**: Silences audio on dead frequencies
- **Dynamic normalization**: `ffmpeg dynaudnorm` smooths loudness across stations
- **Bandwidth selector**: Wide (200 kHz) vs. narrow (130 kHz) for crowded dials
- **Manual gain**: Override AGC from the Quality panel in the UI

---

## Stretch goals / roadmap

- [ ] AirPlay push from Pi (`owntone`)
- [ ] Home Assistant MQTT integration
- [ ] Native iOS/iPadOS app (SwiftUI + AVPlayer)
- [ ] CarPlay support
- [ ] Scheduled recordings
- [ ] Spectrum waterfall display

---

## Troubleshooting

**No audio / FIFO stall**: The audio FIFO blocks until both reader (ffmpeg) and writer (GNU Radio/rtl_fm) are connected. If tuning seems stuck, check `journalctl -u squelch` for errors.

**`rtl_fm: error -3 (no device found)`**: The kernel DVB driver is loaded. Run:
```bash
sudo modprobe -r dvb_usb_rtl28xxu
# 'blacklist' below is the Linux kernel modprobe directive — the name is fixed
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/rtl-blocklist.conf
```

**Poor AM reception**: AM direct sampling requires `-D 2` (handled automatically), but also needs a long-wire antenna. A 10–20m wire connected to the RTL-SDR's antenna input makes a large difference.

**GNU Radio not found**: `gr-osmosdr` and `gr-rds` must be the versions that match your installed `gnuradio`. On Raspberry Pi OS, `sudo apt-get install gnuradio gr-osmosdr gr-rds` installs compatible versions.

---

## License

MIT
