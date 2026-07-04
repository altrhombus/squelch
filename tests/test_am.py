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
