# Changelog

All notable changes to Squelch are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **Opening the app on a second device cancelled a seek.** Mirroring the
  server's band ran the local band-change path, which POSTed `/seek/stop`.
- **Dial snapped back after tuning.** `/tune` returns before the radio has
  moved, so frames still carrying the old station pulled the dial (and
  band tab) back. The dial now holds a local tune until the server reports
  it (2.5 s cap), and wheel scrolls and tap-to-jump glides count as
  interaction too.
- **Paused player kept the SDR running.** A paused `<audio>` holds its
  `/stream` connection, which the server counts as a listener. Stopping
  now releases the stream after 30 s (a quick lock-screen pause/resume
  still works).
- **Playing a recording hijacked live tuning.** Tapping a preset retuned
  the radio but kept playing the recording, and lock-screen play resumed
  live audio instead of the recording. Now a tune switches to the live
  stream, the recording's title isn't overwritten by live metadata, and
  play resumes whichever source was playing.
- **Space fired twice.** On a focused button, Space activated the button
  *and* toggled play/stop.
- **Auto-HD could start audio by itself** from a WebSocket frame (outside
  any user gesture) — it now only retunes.
- **HD sub-channel chips were rebuilt every frame**, eating taps and
  keyboard focus; preset marks on the dial went stale after a band change;
  tapping a chevron didn't stop a running seek; history showed WX channels
  at one decimal (162.525 and 162.550 both as "162.5"); "Recording saved"
  showed even when nothing was recording.
- **Recordings served as `audio/mp4`.** They are raw ADTS; now
  `audio/aac`, which Safari needs to play them.

### Accessibility

- Station name and track info (aria-live regions) are only rewritten when
  their text changes, so screen readers stop re-announcing every frame.
- The frequency dial reports `aria-valuetext` ("91.1 MHz") and supports
  PageUp/PageDown/Home/End; the album-art link activates with Enter.
- JS dial animation and momentum respect `prefers-reduced-motion`.
- MediaSession metadata is only replaced on change (no lock-screen
  flicker), with correct artwork types.
- Web manifest declares `id` and `scope`.
- **XSS via off-air text.** `esc()` in the web app escaped `& < >` but not
  quotes, while station names and titles (RDS PS/RadioText, HD metadata)
  were interpolated into quoted `aria-label` attributes. A crafted
  RadioText could break out of the attribute and run script. Quotes are
  now escaped too.
- **pyfftw was never used.** `scipy.fft.set_backend` is a context manager;
  the bare call left pocketfft in charge while logging "FFTW backend
  active". Now `set_global_backend`.
- **Station name flicker after a PS paging burst.** Stale change times
  re-tripped the "dynamic PS" regime on every reception once it cleared,
  alternately blanking and restoring the name.
- **Seek-down wrap landed off the channel grid.** Wrapping from 87.5 to
  108.0 put every later hop on even tenths, 100 kHz off US stations.
  Wraps now move by whole channels (87.5 ↓ 107.9).
- **Install script always rebuilt librtlsdr.** Its "already installed"
  check grepped `rtl_test` output for a symbol name it never prints; it
  now inspects the library's exports. The nrsc5 step no longer reinstalls
  apt's `librtlsdr-dev` beside the fork, and the fork's udev rules are
  installed so the service user can open the dongle.
- **One sleeping client stalled metadata for everyone.** WebSocket sends
  were awaited serially with no timeout, and every RDS change spawned an
  untracked broadcast task, so a phone with a full TCP buffer blocked all
  updates while tasks piled up. Sends now run concurrently with a 2 s
  deadline (stuck clients are closed and reconnect), and fire-and-forget
  broadcasts coalesce into a single in-flight task.
- **Encoder closed mid-encode on retune.** Cancelling the pipeline closed
  the PyAV encoder on the event loop while the DSP thread could still be
  encoding with it. The close now runs on the DSP thread, behind any
  in-flight block.
- **Old station's RDS credited to the new one.** Queued RDS blocks read
  the *current* decoder and generation when they ran, so groups captured
  before a retune fed the new station's decoder. Each block and callback
  is now bound to the decoder that produced it, and a busy RDS thread
  drops blocks instead of queueing them without bound.
- **SDR setup failure was a permanent outage.** A failure configuring the
  tuner after open left the USB interface claimed and ended the pipeline
  in "error". Setup is now inside the cleanup path, and sessions retry
  with backoff (1–30 s). `sdr.device_index` is now honoured for analog
  bands (it only reached nrsc5 before).
- **Retune wrote the tuner from the HTTP handler**, concurrently with USB
  bulk reads. It now hands the frequency to the session loop, which
  applies it between reads like gain, AFC and seek already do.
- **`systemctl stop` hung with a listener connected.** `/stream` never ends
  on its own and uvicorn waited on it indefinitely, so systemd SIGKILLed
  the process and shutdown cleanup (recording finalisation, DB close)
  never ran. Graceful shutdown is now bounded to 5 s, each cleanup step
  runs even if an earlier one fails, and the unit sets `TimeoutStopSec`.
- **Recorder edge cases.** A failed file open left a phantom listener that
  kept the SDR awake; a scheduled recording could stop a manual one the
  user started during its settle window; an explicit filename could
  overwrite an existing recording.

- **Seek scan rebuilt server-side.** The old client-driven seek polled the
  1 Hz signal-bars estimate on a 750 ms timer — a race that read the
  *previous* frequency's signal and flew past real stations. Seeking now
  runs inside the pipeline loop, evaluating each channel's own per-block
  pilot/noise metrics two-plus blocks after each hop (exactly synchronised
  with the tuner, and the tuner is only touched between USB reads). Audio
  is muted during the sweep and the dial needle follows on every client;
  `POST /seek` / `POST /seek/stop` drive it, with a `seeking` flag on the
  metadata feed.
- **SDR stall watchdog.** Heavy retune churn (e.g. the old rapid-fire seek)
  could wedge the RTL2832U's USB streaming — device open, zero samples,
  audio dead until a physical replug. The session loop now times out after
  8 s of no IQ, closes and reopens the device, and logs a clear message
  (with a replug hint if it recurs).

- **WX band now uses the correct demodulator.** NOAA weather radio is
  narrowband FM (5 kHz deviation); it was routed through the broadcast
  WFM stereo demod, producing ~15× under-deviated audio with 10 kHz of
  needless noise bandwidth. WX now decodes via the NFM path, mono.
- **Block-edge artifacts eliminated across the DSP.** All resampling and
  carrier-recovery stages are now stateful (`backend/sdr/dsp.py`:
  `StatefulResampler`, `PilotRecovery`), the FM discriminator carries its
  last sample across blocks, and the ×5 audio decimation keeps a
  continuous phase. Previously each ~218 ms block restarted the
  resampler FIR and FFT-hilbert, injecting a ~4.6 Hz edge transient —
  marginal in audio, but corrupting RDS bits at every block boundary.
- **AM adjacent-channel whistles removed.** The AM path had no channel
  filter, so the ±24 kHz decimated passband held two neighbouring
  stations per side whose carriers beat as 10/20 kHz heterodynes; a
  ±5.5 kHz channel filter plus a 5 kHz audio lowpass now isolate the
  tuned station. NFM similarly gains a ±8 kHz (Carson bandwidth)
  channel filter.
- **Executor-thread leak on every tune.** Each tune created a new
  pipeline whose three thread pools were never shut down (four leaked
  threads per tune); `RadioPipeline.close()` now releases them.
- Retuning no longer silently discards all subsequent RDS metadata (the
  pipeline's tune-generation stamp was not refreshed on retune).
- Mono-mode listening on a strong signal no longer gets the conservative
  weak-signal Wiener noise floor (quality now derives from the measured
  SNR gate rather than the stereo blend factor, which mono mode pins to 0).

### Changed

- systemd unit: `SupplementaryGroups=plugdev`, `NoNewPrivileges`,
  `PrivateTmp`, `ProtectSystem=full`.
- **RDS weak-signal sensitivity substantially improved**: burst error
  correction (≤2-bit bursts via the (26,16) code's syndrome table, gated
  to expected block offsets while synced), position-tracked block sync
  that holds bit alignment through CRC failures instead of re-acquiring
  on any single bit error, and adaptive symbol-timing recovery
  (per-phase energy tracking, biphase-lobe-aware) that handles arbitrary
  start phase and SDR clock ppm drift. End-to-end synthetic tests decode
  97% of groups at 40 ppm clock error, and 97% under noise + drift
  conditions that previously decoded nothing.
- **The SDR is now fully closed while idle** — tuner powered off, USB
  DMA stopped — instead of discarding IQ with only the DSP suspended.
  The dongle is typically the hottest component in the enclosure, so
  this is the largest average-temperature win available. The device
  reopens automatically on the next listener (Icecast `keep_alive: true`
  still holds it open).
- **Same-band FM retunes are now seamless**: the tuner hops and fresh
  demod/RDS/HD-detect state is created inside the running pipeline; the
  SDR session, encoder, and client connections stay up (previously every
  dial step tore down and rebuilt the entire stack).
- FM pilot/carrier recovery switched from per-block FFT hilbert to
  heterodyne + stateful lowpass (`PilotRecovery`) — phase-continuous
  across blocks and cheaper: no large FFTs remain in the demod hot path.
- Internal restructuring: API endpoints split into per-domain routers
  (`backend/routes/`), app singletons moved into the lifespan
  (`backend/context.py`), one shared SQLite connection (WAL mode, indexed
  history) instead of per-request connections, atomic cover-art writes,
  and a configurable database path (`database.path` in settings.yaml).
  No changes to the HTTP API.
- **License: GPL-2.0 → GPL-3.0-or-later.** The previous bare GPLv2 text had
  no copyright notice and was incompatible with pyrtlsdr (GPLv3), which
  Squelch imports as a library. All code to date is by the sole author, so
  relicensing is clean.

### Added

- **Complete UI redesign in the macOS 27 "Golden Gate" design language.**
  One continuous radio surface replaces the three-panel dashboard: a
  scrub-able glass frequency ruler (momentum + channel snapping, preset
  stations marked on the tape, tap-to-jump) under a large tabular-numeral
  readout, with the now-playing hero and transport on the same screen.
  Library (presets/history/recordings) becomes a flush edge sidebar on
  desktop and a second tab on phone. Diagnostics is a popover on the
  signal indicator; a Clear/Regular/Tinted glass-intensity setting
  (persisted) mirrors Golden Gate's system slider and doubles as the
  reduced-transparency story alongside `prefers-contrast` and
  `prefers-reduced-motion` support, concentric corner radii, and
  adaptive-contrast floating chrome.
- **AM and Scanner bands in the UI** (they existed backend-only): AM with
  kHz ruler and 10 kHz steps; Scanner with a frequency keypad; both WX
  and Scanner surface a squelch slider (the `/squelch` endpoint
  previously had no UI). The seeded AM preset no longer crashes the tuner.
- **Hold-to-seek**: press-and-hold a step chevron scans the band and
  stops on the next receivable station (client-driven signal polling).
- Signal bars now reflect FM reception quality (discriminator noise vs
  pilot — what you hear) instead of the AGC-regulated IQ level.
- UI fixes folded in: retuning no longer restarts the audio element
  (seamless dial steps), arrow-key tuning is debounced, the volume
  slider is hidden on iOS where it is inert, stations without RDS fall
  back from "Waiting for station info…" to "On air", and recording
  state re-syncs when the tab regains focus.
- AFC for narrowband bands (WX/scanner): the pipeline recentres the tuner
  on the measured carrier when a stable offset > 1 kHz is detected (up to
  two hops per session, noise- and squelch-proof), so an uncalibrated
  dongle's crystal error no longer parks NFM/AM-scanner signals outside
  the channel filter. `ppm_correction` becomes an optimisation rather
  than a requirement.
- Spectral noise reduction on the NFM path (WX/scanner voice): the FM
  Wiener subtractor driven by a discriminator noise-floor measurement
  above the voice band (6–20 kHz) — ~7 dB cleaner speech gaps in synthetic
  tests. Known caveat: stationary tones longer than ~1.4 s (NOAA alert
  tone) ride at the −12 dB floor but stay clearly audible post-AGC.
- ppm self-calibration diagnostics: `diag.pilot_offset_hz` (FM — the
  19 kHz pilot is transmitter-exact to ±2 Hz) and `diag.carrier_offset_hz`
  (NFM/WX — power-centroid carrier offset; NOAA carriers are exact),
  measured live to calibrate `settings.yaml` `ppm_correction` without
  stopping the service. Also fixed: `ppm_correction` was read from config
  but never applied to the SDR.
- Dynamic-PS reassembly: stations that page now-playing text through the RDS
  PS field in 8-character chunks (instead of using RadioText) now get
  artist/title reconstructed via a successor-graph model (page-to-page
  evidence accumulated across lossy passes — tolerates heavy page loss on
  marginal signals), with the paged fragments no longer shown as station
  names
- Artist/title order auto-correction: RDS has no defined order and stations
  transmit both "Artist - Title" and "Title - Artist"; the iTunes lookup's
  canonical names are used to detect and swap reversed fields before the
  history save
- Art-source precedence: HD Radio LOT artwork always supersedes iTunes
  search artwork (including when LOT lands mid-lookup), and iTunes art now
  refreshes on song changes instead of sticking
- `metadata:` config section: `itunes_lookup` (privacy opt-out; also
  disables order correction), `order_correction`, and `show_ps_messages`
  (display non-song PS messages like show promos on the track line —
  garbled fragments are always filtered regardless)
- Tiered RadioText emission for weak signals: messages ending in the 0x0D
  terminator complete as soon as all segments up to it arrive (a 23-char
  message needs 6 segments, not 16); still-incomplete text is shown
  partially (gaps as spaces) after 15 s and fills in progressively
- Confidence-gated persistence: provisional data (partial RadioText,
  single-evidence PS assembly) displays immediately but never reaches
  history or the iTunes lookup — history is written once, from confident
  data, instead of write-then-fix
- Single-tuner HD Radio detection: IBOC digital sidebands (±135–195 kHz)
  are sniffed from the raw IQ while listening to analog FM; the UI shows a
  tappable "HD available" badge that switches to HD mode. Detection ratio
  exposed as `hd_ratio` in diagnostics for threshold calibration
- Icecast2 output: pushes the AAC stream to an Icecast mount with live
  now-playing metadata from RDS/HD Radio. `icecast.keep_alive` controls
  whether the mount stays live only while listeners are active (default,
  preserves DSP idle-suspend) or whenever a station is tuned.

### Removed

- Dead config keys that were never read (`audio:` section,
  `recordings.default_bitrate`) and the unused `bandwidth` API parameter

### Fixed

- README DSP tuning reference brought back in sync with the code
  (`_GAIN_HOLD_BLOCKS`, block interval, adaptive Wiener floor)

## [0.1.0] — 2026-07-02

First tagged release.

### Features

- FM stereo with a custom numpy/scipy DSP pipeline: pilot demodulation,
  Ephraim-Malah Wiener noise reduction, stereo blend, de-emphasis,
  K-weighted AGC, soft-knee limiter
- RDS metadata (PS, RadioText, RT+, PTY) with fuzzy history deduplication
- HD Radio via nrsc5 (multi-subchannel, cover art, rich metadata)
- AM (direct sampling), NOAA weather band, and scanner/NFM with aviation-band
  AM auto-switching
- AAC-LC chunked HTTP streaming — native playback on iOS/macOS/Chrome, AirPlay
- One-tap and cron-scheduled recordings, auto-named from station metadata
- Mobile-friendly web UI: presets, tuning dial, signal meters, ambient art,
  iOS Media Session integration
- Software gain control tuned for FM SNR rather than ADC headroom
- DSP idle suspend when no clients are connected (Pi thermals)

### Security

- Recording filenames from the API are sanitized and pinned to the recordings
  directory
- Documented the trusted-LAN threat model (see README *Security*)

[0.1.0]: https://github.com/altrhombus/squelch/releases/tag/v0.1.0
