"""AM / NFM demodulator tests with synthetic IQ.

The AM test targets the adjacent-channel defect: US AM stations sit
10 kHz apart, and without a channel filter the ±24 kHz decimated
passband contains two neighbours per side, whose carriers beat against
the envelope detector as loud heterodyne whistles.
"""

import numpy as np

from backend.sdr.am import AmDemodulator, NfmDemodulator, _SAMPLE_RATE

BLOCK = 131_072   # pipeline AM/scanner/WX block size
N_BLOCKS = 8
AUDIO_RATE = 48_000


def tone_level(x: np.ndarray, freq: float) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / AUDIO_RATE)
    band = (freqs > freq - 60) & (freqs < freq + 60)
    return float(spectrum[band].max())


def run_blocks(demod, iq):
    out = None
    for b in range(N_BLOCKS):
        out = demod.process(iq[b * BLOCK:(b + 1) * BLOCK])
    return out   # last block, AGC settled


def test_am_recovers_tone_and_rejects_adjacent_channel():
    n = BLOCK * N_BLOCKS
    t = np.arange(n) / _SAMPLE_RATE
    # Tuned station: 1 kHz programme, 50% modulation
    tuned = (1.0 + 0.5 * np.sin(2 * np.pi * 1_000 * t))
    # Strong adjacent channel at +10 kHz — its carrier beats at 10 kHz in
    # an unfiltered envelope detector.
    adjacent = 0.8 * (1.0 + 0.5 * np.sin(2 * np.pi * 2_000 * t)) \
        * np.exp(2j * np.pi * 10_000 * t)
    iq = (0.3 * (tuned + adjacent)).astype(np.complex64)

    audio = run_blocks(AmDemodulator(), iq)

    assert audio.dtype == np.float32
    assert np.all(np.isfinite(audio))
    want = tone_level(audio, 1_000)
    whistle = tone_level(audio, 10_000)
    rejection_db = 20 * np.log10(want / (whistle + 1e-12))
    assert rejection_db > 30.0, f"adjacent carrier only {rejection_db:.1f} dB down"


def test_nfm_recovers_voice_tone():
    n = BLOCK * N_BLOCKS
    t = np.arange(n) / _SAMPLE_RATE
    # 5 kHz deviation NFM (NOAA WX style), 1 kHz tone
    phase = 2 * np.pi * 5_000 * np.cumsum(np.sin(2 * np.pi * 1_000 * t)) / _SAMPLE_RATE
    iq = (0.3 * np.exp(1j * phase)).astype(np.complex64)

    audio = run_blocks(NfmDemodulator(), iq)

    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    freqs = np.fft.rfftfreq(len(audio), 1 / AUDIO_RATE)
    spectrum[freqs < 100] = 0
    assert abs(freqs[np.argmax(spectrum)] - 1_000) < 30


def test_nfm_carrier_offset_estimator():
    """A carrier 7 kHz off the tuned frequency must be reported — this is
    the ppm-calibration instrument for WX (NOAA carriers are exact)."""
    n = BLOCK * 4
    t = np.arange(n) / _SAMPLE_RATE
    phase = 2 * np.pi * 5_000 * np.cumsum(np.sin(2 * np.pi * 1_000 * t)) / _SAMPLE_RATE
    iq = (0.3 * np.exp(1j * (2 * np.pi * 7_000 * t + phase))).astype(np.complex64)

    demod = NfmDemodulator()
    for b in range(4):
        demod.process(iq[b * BLOCK:(b + 1) * BLOCK])
    assert abs(demod.last_carrier_offset_hz - 7_000) < 100


def test_nfm_noise_reduction_lowers_noise_floor():
    """The spectral subtractor on the NFM path must cut inter-formant
    noise without touching the programme tone (SNR ratio, so the two
    runs' AGC differences cancel).

    The tone is burst-modulated at word cadence (250 ms on/off): MinStat
    keys on the gaps between speech to find the noise floor — a
    never-pausing tone is its documented blind spot, not the use case."""
    rng = np.random.default_rng(3)
    n = BLOCK * 16   # MinStat needs ~1.4 s to fill its window
    t = np.arange(n) / _SAMPLE_RATE
    bursts = ((t % 0.5) < 0.25).astype(np.float64)   # last block falls on ON
    mod = bursts * np.sin(2 * np.pi * 1_000 * t)
    phase = 2 * np.pi * 4_000 * np.cumsum(mod) / _SAMPLE_RATE
    noise = 0.05 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    iq = (0.3 * np.exp(1j * phase) + noise).astype(np.complex64)

    def run(nr: bool):
        d = NfmDemodulator(noise_reduction=nr)
        return [d.process(iq[b * BLOCK:(b + 1) * BLOCK]) for b in range(16)]

    def band_power(x, lo, hi):
        s = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
        f = np.fft.rfftfreq(len(x), 1 / AUDIO_RATE)
        return float(s[(f > lo) & (f < hi)].mean())

    clean, raw = run(True), run(False)
    # Block 12 (~1.31–1.42 s) sits inside an OFF gap — residual hiss the
    # NR should crush; block 15 is ON — the tone it must preserve.
    # Ratio form cancels each run's overall AGC scaling.
    snr_nr  = band_power(clean[15], 950, 1050) / band_power(clean[12], 500, 3400)
    snr_raw = band_power(raw[15],   950, 1050) / band_power(raw[12],   500, 3400)
    assert snr_nr > snr_raw * 2, f"NR gained only {10*np.log10(snr_nr/snr_raw):.1f} dB"


class _FakeSdr:
    def __init__(self, center=162.4e6):
        self.center_freq = center


def _afc_pipeline():
    from backend.sdr.pipeline import RadioPipeline

    class _Est:
        offset_hz = 0.0
        updates = 0
        def reset(self):
            pass

    class _Demod:
        pass

    p = RadioPipeline({}, None, None)
    est = _Est()
    d = _Demod()
    d.carrier_offset = est
    p._demod = d
    p._afc_hops_left = 2
    p._afc_hist = []
    p._afc_last_updates = -1
    return p, est


def test_afc_recenters_on_stable_offset():
    import asyncio
    p, est = _afc_pipeline()
    try:
        sdr = _FakeSdr()
        est.offset_hz = -16_360.0
        for _ in range(4):
            est.updates += 1
            p._afc_step(sdr)
        assert abs(sdr.center_freq - (162.4e6 - 16_360)) < 1.0
        assert p._afc_hops_left == 1
    finally:
        asyncio.run(p.close())


def test_afc_holds_on_noise_stale_and_centred():
    import asyncio
    p, est = _afc_pipeline()
    try:
        sdr = _FakeSdr()
        # Unstable readings (noise centroids scatter): no hop.
        for off in (-3_000.0, 5_000.0, -12_000.0, 800.0, 9_000.0):
            est.offset_hz = off
            est.updates += 1
            p._afc_step(sdr)
        assert sdr.center_freq == 162.4e6 and p._afc_hops_left == 2
        # Stale readings (updates frozen, e.g. squelched): ignored.
        est.offset_hz = -16_000.0
        for _ in range(8):
            p._afc_step(sdr)
        assert sdr.center_freq == 162.4e6
        # Stable and already centred: AFC declares itself done.
        est.offset_hz = 120.0
        for _ in range(4):
            est.updates += 1
            p._afc_step(sdr)
        assert sdr.center_freq == 162.4e6 and p._afc_hops_left == 0
    finally:
        asyncio.run(p.close())


def test_wx_band_routes_to_nfm():
    """NOAA weather is NFM — routing it through the WFM stereo demod
    produced ~15× under-deviated audio plus 10 kHz of noise bandwidth."""
    import asyncio
    from backend.sdr.pipeline import RadioPipeline

    p = RadioPipeline({}, None, None)
    try:
        assert isinstance(p._make_demod("wx", 162.55e6, 75), NfmDemodulator)
        assert isinstance(p._make_demod("am", 1.0e6, 75), AmDemodulator)
        # Aviation scanner frequencies use AM; others NFM
        assert isinstance(p._make_demod("scanner", 121.5e6, 75), AmDemodulator)
        assert isinstance(p._make_demod("scanner", 155.0e6, 75), NfmDemodulator)
    finally:
        asyncio.run(p.close())
