# Changelog

All notable changes to Squelch are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

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
