# Changelog

All notable changes to Squelch are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and versions follow
[Semantic Versioning](https://semver.org/).

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
