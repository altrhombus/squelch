"""Server-side FM seek-scan logic (backend/sdr/pipeline.py).

Drives _seek_step directly with a stub demodulator exposing the same
per-block pilot/noise metrics the real FM demod publishes, plus a fake
SDR whose center_freq the seek moves.  No hardware, no event loop timing.
"""

import asyncio


from backend.metadata import MetadataState
from backend.sdr.pipeline import (
    RadioPipeline,
    _SEEK_STEP_HZ,
    _SEEK_SETTLE_BLKS,
    _SEEK_FM_LO,
    _SEEK_FM_HI,
)


class _FakeSdr:
    def __init__(self, freq):
        self.center_freq = freq


class _FakeTask:
    """A running task, from start_seek's point of view (never done)."""
    def done(self):
        return False


class _StubDemod:
    """Minimal stand-in exposing the metrics _seek_step reads."""
    def __init__(self, pilot=0.0, noise=0.02):
        self.last_pilot_rms = pilot
        self.last_noise_rms = noise
        self.last_iq_rms = 0.1


def _pipeline(freq=98.1e6):
    meta = MetadataState({})
    meta.update_tune(freq, "fm")
    p = RadioPipeline({}, meta, None)
    p._band = "fm"
    p._freq = freq
    p._task = _FakeTask()   # not-done, so start_seek proceeds
    return p, meta


def _close(p):
    p._task = None          # avoid stop() awaiting the fake task
    asyncio.run(p.close())


def _drive(p, sdr, steps):
    """Run N _seek_step calls (each is one processed block)."""
    for _ in range(steps):
        if p._seek is None:
            break
        asyncio.run(p._seek_step(sdr))


def test_start_seek_arms_and_flags():
    p, meta = _pipeline()
    try:
        assert p.start_seek(+1) is True
        assert p._seek is not None
        assert p._seek["dir"] == 1
        assert meta.seeking is True
    finally:
        _close(p)


def test_seek_hops_past_empty_channels():
    p, meta = _pipeline(freq=98.1e6)
    try:
        p._demod = _StubDemod(pilot=0.0)   # nothing here
        p.start_seek(+1)
        sdr = _FakeSdr(p._freq)
        start = p._freq
        # settle blocks produce no decision, then one hop
        _drive(p, sdr, _SEEK_SETTLE_BLKS + 1)
        assert p._seek is not None                     # still scanning
        assert abs(sdr.center_freq - (start + _SEEK_STEP_HZ)) < 1
        assert abs(p._freq - (start + _SEEK_STEP_HZ)) < 1
        assert meta.frequency == p._freq               # dial mirror follows
    finally:
        _close(p)


def test_seek_stops_on_listenable_station():
    p, meta = _pipeline()
    try:
        p._demod = _StubDemod(pilot=0.07, noise=0.01)   # clean stereo pilot
        p.start_seek(+1)
        sdr = _FakeSdr(p._freq)
        _drive(p, sdr, _SEEK_SETTLE_BLKS + 1)
        assert p._seek is None
        assert meta.seeking is False
        # a fresh demod + RDS were installed for the found station
        from backend.sdr.fm import FmStereoDemodulator
        assert isinstance(p._demod, FmStereoDemodulator)
        assert p._rds is not None
    finally:
        _close(p)


def test_seek_down_wraps_at_band_edge():
    p, meta = _pipeline(freq=_SEEK_FM_LO)
    try:
        p._demod = _StubDemod(pilot=0.0)
        p.start_seek(-1)
        sdr = _FakeSdr(p._freq)
        _drive(p, sdr, _SEEK_SETTLE_BLKS + 1)
        # stepping below the bottom wraps to the top of the band
        assert abs(sdr.center_freq - _SEEK_FM_HI) < 1
    finally:
        _close(p)


def test_seek_exhausts_and_stops():
    p, meta = _pipeline()
    try:
        p._demod = _StubDemod(pilot=0.0)   # empty band — never finds anything
        p.start_seek(+1)
        sdr = _FakeSdr(p._freq)
        # far more than a full sweep; it must terminate itself
        _drive(p, sdr, (int((_SEEK_FM_HI - _SEEK_FM_LO) / _SEEK_STEP_HZ) + 5)
               * (_SEEK_SETTLE_BLKS + 1))
        assert p._seek is None
        assert meta.seeking is False
    finally:
        _close(p)


def test_stop_seek_clears_flag():
    p, meta = _pipeline()
    try:
        p._demod = _StubDemod(pilot=0.0)
        p.start_seek(+1)
        assert meta.seeking is True
        asyncio.run(p.stop_seek())
        assert p._seek is None
        assert meta.seeking is False
    finally:
        _close(p)


def test_start_seek_refused_when_not_fm():
    p, meta = _pipeline()
    try:
        p._band = "am"
        assert p.start_seek(+1) is False
        assert p._seek is None
    finally:
        _close(p)
