"""HD sideband detector tests with synthetic IQ spectra."""

import numpy as np

from backend.sdr.hd_detect import HdSidebandDetector, _FS, _NFFT, _SEGS

N = _NFFT * _SEGS
RNG = np.random.default_rng(42)


def analog_fm(n=N, amp=0.3):
    """FM carrier modulated with a 1 kHz tone at ±75 kHz deviation —
    occupies roughly ±100 kHz, nothing in the sideband region."""
    t = np.arange(n) / _FS
    mpx = 0.9 * np.sin(2 * np.pi * 1000 * t)
    phase = 2 * np.pi * 75_000 * np.cumsum(mpx) / _FS
    return (amp * np.exp(1j * phase)).astype(np.complex64)


def noise_floor(n=N, level=0.002):
    return (level * (RNG.standard_normal(n) + 1j * RNG.standard_normal(n))).astype(np.complex64)


def sideband(n=N, center=165_000.0, bw=60_000.0, level=0.02):
    """Flat noise-like OFDM stand-in: band-limited noise shifted to `center`."""
    t = np.arange(n) / _FS
    # Band-limit by low-passing white noise with a boxcar in the freq domain
    white = RNG.standard_normal(n) + 1j * RNG.standard_normal(n)
    spec = np.fft.fft(white)
    freqs = np.fft.fftfreq(n, 1.0 / _FS)
    spec[np.abs(freqs) > bw / 2] = 0
    baseband = np.fft.ifft(spec)
    baseband /= np.sqrt(np.mean(np.abs(baseband) ** 2)) + 1e-20
    return (level * baseband * np.exp(2j * np.pi * center * t)).astype(np.complex64)


def run(iq, passes=6):
    det = HdSidebandDetector()
    for _ in range(passes):
        det.process(iq)
    return det


def test_analog_only_not_detected():
    det = run(analog_fm() + noise_floor())
    assert det.available is False
    assert det.ratio < det.OFF_RATIO


def test_hybrid_iboc_detected():
    iq = (analog_fm() + noise_floor()
          + sideband(center=+165_000) + sideband(center=-165_000))
    det = run(iq)
    assert det.available is True


def test_one_sided_energy_rejected():
    """Adjacent-channel splatter lands on one side only — must not trigger."""
    iq = analog_fm() + noise_floor() + sideband(center=+165_000)
    det = run(iq)
    assert det.available is False


def test_weak_sidebands_below_threshold():
    iq = (analog_fm() + noise_floor(level=0.02)      # high noise floor
          + sideband(center=+165_000, level=0.004)   # sidebands barely above it
          + sideband(center=-165_000, level=0.004))
    det = run(iq)
    assert det.available is False


def test_hysteresis_holds_through_momentary_dropout():
    with_hd = (analog_fm() + noise_floor()
               + sideband(center=+165_000) + sideband(center=-165_000))
    det = run(with_hd, passes=6)
    assert det.available is True
    det.process(analog_fm() + noise_floor())   # one bad measurement
    assert det.available is True               # EMA + hysteresis rides it out


def test_short_block_ignored():
    det = HdSidebandDetector()
    det.process(np.zeros(100, np.complex64))   # must not raise
    assert det.available is False
