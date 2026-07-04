"""
End-to-end FM stereo demodulator test with a synthetic MPX signal.

A textbook FM stereo multiplex is generated in numpy — L+R baseband, DSB-SC
L−R subcarrier at 38 kHz, 19 kHz pilot at 10% injection — FM-modulated at
±75 kHz deviation onto a 1.2 MHz complex baseband, then fed through
FmStereoDemodulator block by block exactly as the pipeline would.

No SDR hardware required.
"""

import numpy as np
import pytest

from backend.sdr.fm import FmStereoDemodulator, _MAX_DEV, _SAMPLE_RATE

BLOCK = 262_144      # same block size the pipeline uses
N_BLOCKS = 8         # ~1.7 s — enough for AGC warmup and Wiener/MinStat to settle
F_LEFT, F_RIGHT = 1000.0, 2500.0
AUDIO_RATE = 48_000


def make_iq(stereo: bool, amp_iq: float = 0.3, tone_amp: float = 0.5,
            pilot_hz: float = 19_000.0) -> np.ndarray:
    n = BLOCK * N_BLOCKS
    t = np.arange(n) / _SAMPLE_RATE
    left = tone_amp * np.sin(2 * np.pi * F_LEFT * t)
    right = tone_amp * np.sin(2 * np.pi * F_RIGHT * t)
    theta = 2 * np.pi * pilot_hz * t
    if stereo:
        mpx = (0.45 * (left + right)
               + 0.45 * (left - right) * np.cos(2 * theta)   # DSB-SC at 38 kHz
               + 0.1 * np.cos(theta))                        # pilot, 10% injection
    else:
        mpx = 0.9 * left
    phase = 2 * np.pi * _MAX_DEV * np.cumsum(mpx) / _SAMPLE_RATE
    return (amp_iq * np.exp(1j * phase)).astype(np.complex64)


def run_demod(iq: np.ndarray) -> tuple:
    demod = FmStereoDemodulator(deemphasis_us=75)
    left = right = None
    for b in range(N_BLOCKS):
        left, right, _composite = demod.process(iq[b * BLOCK:(b + 1) * BLOCK])
    return demod, left, right  # last block, fully settled


def tone_amplitude(x: np.ndarray, freq: float) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / AUDIO_RATE)
    band = (freqs > freq - 50) & (freqs < freq + 50)
    return float(spectrum[band].max())


def peak_freq(x: np.ndarray) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / AUDIO_RATE)
    spectrum[freqs < 100] = 0  # ignore DC/LF residue
    return float(freqs[np.argmax(spectrum)])


@pytest.fixture(scope="module")
def stereo_result():
    return run_demod(make_iq(stereo=True))


@pytest.fixture(scope="module")
def mono_result():
    return run_demod(make_iq(stereo=False))


# ---------------------------------------------------------------------------
# Stereo decode
# ---------------------------------------------------------------------------

class TestStereo:
    def test_pilot_detected_at_expected_level(self, stereo_result):
        demod, _, _ = stereo_result
        # 10% pilot → RMS = 0.1/√2 ≈ 0.0707 (probe measured 0.0700)
        assert 0.05 < demod.last_pilot_rms < 0.09

    def test_blend_reaches_stereo(self, stereo_result):
        demod, _, _ = stereo_result
        assert demod.last_blend > 0.5  # probe: 0.82 on a clean signal

    def test_each_channel_recovers_its_tone(self, stereo_result):
        _, left, right = stereo_result
        assert abs(peak_freq(left) - F_LEFT) < 30
        assert abs(peak_freq(right) - F_RIGHT) < 30

    def test_channel_separation(self, stereo_result):
        _, left, right = stereo_result
        # Probe measured +21.9 dB (1 kHz) and +9.3 dB (2.5 kHz); assert with margin
        sep_l = 20 * np.log10(tone_amplitude(left, F_LEFT) / tone_amplitude(right, F_LEFT))
        sep_r = 20 * np.log10(tone_amplitude(right, F_RIGHT) / tone_amplitude(left, F_RIGHT))
        assert sep_l > 12.0
        assert sep_r > 6.0

    def test_output_is_valid_audio(self, stereo_result):
        _, left, right = stereo_result
        for ch in (left, right):
            assert ch.dtype == np.float32
            assert np.all(np.isfinite(ch))
            assert np.max(np.abs(ch)) <= 1.0  # limiter ceiling


# ---------------------------------------------------------------------------
# Mono fallback (no pilot)
# ---------------------------------------------------------------------------

class TestMono:
    def test_no_pilot_means_no_blend(self, mono_result):
        demod, _, _ = mono_result
        assert demod.last_pilot_rms < 0.01
        assert demod.last_blend < 0.1

    def test_channels_identical(self, mono_result):
        _, left, right = mono_result
        assert float(np.corrcoef(left, right)[0, 1]) > 0.999

    def test_tone_recovered(self, mono_result):
        _, left, _ = mono_result
        assert abs(peak_freq(left) - F_LEFT) < 30


# ---------------------------------------------------------------------------
# Forced modes
# ---------------------------------------------------------------------------

def test_pilot_offset_estimator_reads_crystal_error():
    """A pilot 0.8 Hz off nominal (≈ −42 ppm crystal) must be reported by
    the diag estimator — this drives ppm_correction self-calibration."""
    iq = make_iq(stereo=True, pilot_hz=19_000.8)
    demod, _, _ = run_demod(iq)
    assert abs(demod.last_pilot_offset_hz - 0.8) < 0.1


def test_stereo_mode_mono_forces_identical_channels():
    iq = make_iq(stereo=True)
    demod = FmStereoDemodulator(deemphasis_us=75, stereo_mode="mono")
    left = right = None
    for b in range(N_BLOCKS):
        left, right, _ = demod.process(iq[b * BLOCK:(b + 1) * BLOCK])
    assert float(np.corrcoef(left, right)[0, 1]) > 0.999
