# Changelog

All notable changes to Squelch are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

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
