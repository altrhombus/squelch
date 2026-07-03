# Contributing to Squelch

Thanks for your interest! Contributions of all kinds are welcome — bug reports, DSP improvements, UI polish, and documentation.

## Development setup

You **do not need SDR hardware** to work on most of Squelch. The test suite generates synthetic FM/IQ signals in numpy, and everything outside the RTL-SDR read loop runs on any Linux or macOS machine.

```bash
git clone https://github.com/altrhombus/squelch.git
cd squelch
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

To run the full app you need a Raspberry Pi (or any Linux box) with an RTL-SDR dongle — see the README's installation section.

## Running tests and lint

```bash
.venv/bin/pytest -q                      # full suite, ~5 s, no hardware needed
.venv/bin/ruff check backend/ tests/     # lint
node --check frontend/app.js             # frontend syntax
```

CI runs the same three commands on every PR. Please make sure they pass locally first.

### What the tests cover

- `tests/test_dsp.py` — synthesizes a textbook FM stereo multiplex (pilot, DSB-SC subcarrier) and runs it through the full demodulator; asserts tone recovery, channel separation, and mono fallback. If you touch `backend/sdr/fm.py`, these tests are your safety net.
- `tests/test_metadata.py` — RDS RadioText parsing and fuzzy history matching.
- `tests/test_nrsc5_parser.py` — HD Radio metadata line parsing.
- `tests/test_recorder.py`, `tests/test_presets.py`, `tests/test_streaming.py` — recording, scheduling, preset CRUD, and the AAC/streaming layer.

New features should come with tests. For DSP changes, a synthetic-signal test like the existing ones is ideal; if a change is only verifiable by ear on real broadcasts, say so in the PR and describe what you listened for.

## Code style

- Python: `ruff check` must pass (config in `pyproject.toml`). The codebase uses aligned assignments and detailed comments on DSP constants — match the style of the file you're editing.
- Frontend: vanilla JS/CSS, no build step, no frameworks. Any string that came from RDS/HD metadata must go through `esc()` before touching `innerHTML`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `perf:`, `docs:`, `test:`, `chore:`.

## DSP changes

The FM pipeline's tuning constants are documented in the README's *DSP tuning reference*. If you change a constant or add one:

1. Update that README table (default, effect, when to adjust).
2. Note what hardware/stations you validated against — signal-dependent behavior (weak-signal artifacts, blend thresholds) can't be fully captured in tests.

## Reporting bugs

Please include: Pi model, RTL-SDR version (v3/v4/other), the band and frequency, and relevant output from `journalctl -u squelch`. The issue template will prompt you.
