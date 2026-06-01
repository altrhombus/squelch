"""
FM stereo demodulator.

Input:  complex64 IQ samples at 2.4 MHz (from pyrtlsdr)
Output: (left, right) float32 arrays at 48 kHz + FM composite float32 at 240 kHz for RDS

Pipeline per block:
  IQ (2.4 MHz) → resample ×1/10 → 240 kHz complex
  → phase discriminator → FM composite float
  → L+R: LPF 15 kHz → decimate ×5 → 48 kHz
  → pilot BPF 17-21 kHz → Hilbert → 2× phase → 38 kHz carrier
  → L-R: BPF 23-53 kHz → × carrier → LPF 15 kHz → decimate ×5 → 48 kHz
  → matrix (L+R ± L-R) / 2
  → de-emphasis (75 µs US, first-order IIR)

Filter state is maintained between blocks for seamless streaming.
"""

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, lfilter, resample_poly, hilbert

_SAMPLE_RATE = 2_400_000
_DEMOD_RATE  = 240_000
_AUDIO_RATE  = 48_000
_CHAN_DECIM  = _SAMPLE_RATE // _DEMOD_RATE   # 10
_AUDIO_DECIM = _DEMOD_RATE  // _AUDIO_RATE   # 5
_MAX_DEV     = 75_000                         # FM max deviation Hz


def _zero_zi(sos: np.ndarray) -> np.ndarray:
    return np.zeros((sos.shape[0], 2), dtype=np.float64)


class FmStereoDemodulator:
    """
    Stateful FM stereo demodulator. Call process() once per IQ block.
    """

    SAMPLE_RATE = _SAMPLE_RATE
    DEMOD_RATE  = _DEMOD_RATE
    AUDIO_RATE  = _AUDIO_RATE

    def __init__(self, deemphasis_us: int = 75):
        # --- filter coefficients (computed once) ---
        self._lpr_sos     = butter(4, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        self._pilot_sos   = butter(4, [17_000, 21_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_sos     = butter(4, [23_000, 53_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_lp_sos  = butter(4, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')

        # --- per-block filter states ---
        self._lpr_zi      = _zero_zi(self._lpr_sos)
        self._pilot_zi    = _zero_zi(self._pilot_sos)
        self._lmr_zi      = _zero_zi(self._lmr_sos)
        self._lmr_lp_zi   = _zero_zi(self._lmr_lp_sos)

        # --- de-emphasis IIR (first-order) ---
        dt    = 1.0 / _AUDIO_RATE
        tau   = deemphasis_us * 1e-6
        alpha = dt / (tau + dt)
        self._de_b     = np.array([alpha],          dtype=np.float64)
        self._de_a     = np.array([1.0, -(1-alpha)], dtype=np.float64)
        self._de_l_zi  = np.zeros(1)
        self._de_r_zi  = np.zeros(1)

    def process(self, iq: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Process one IQ block.
        Returns (left_48k, right_48k, composite_240k) as float32.
        """
        # 1. Decimate complex IQ ×10 → 240 kHz (resample_poly anti-aliases correctly)
        demod_iq = resample_poly(iq, 1, _CHAN_DECIM).astype(np.complex64)

        # 2. FM phase discriminator → composite baseband
        z = demod_iq[1:] * np.conj(demod_iq[:-1])
        composite = (np.angle(z) * (_DEMOD_RATE / (2.0 * np.pi * _MAX_DEV))).astype(np.float64)

        # 3. L+R: lowpass 15 kHz + decimate ×5 → 48 kHz
        lpr_full, self._lpr_zi = sosfilt(self._lpr_sos, composite, zi=self._lpr_zi)
        lpr = lpr_full[::_AUDIO_DECIM].astype(np.float32)

        # 4. Pilot: BPF 17–21 kHz for carrier generation
        pilot, self._pilot_zi = sosfilt(self._pilot_sos, composite, zi=self._pilot_zi)

        # 5. L-R subcarrier: BPF 23–53 kHz
        lmr_band, self._lmr_zi = sosfilt(self._lmr_sos, composite, zi=self._lmr_zi)

        # 6. Generate 38 kHz carrier via Hilbert squaring of pilot
        #    hilbert(pilot) ≈ A·e^{jφ}; squaring → A²·e^{j2φ} at 38 kHz
        pilot_a   = hilbert(pilot).astype(np.complex64)
        carrier38 = (pilot_a ** 2).real.astype(np.float64)

        # 7. Coherent demod of DSB-SC L-R, LPF, decimate
        lmr_demod              = lmr_band * carrier38 * 2.0
        lmr_full, self._lmr_lp_zi = sosfilt(self._lmr_lp_sos, lmr_demod, zi=self._lmr_lp_zi)
        lmr = lmr_full[::_AUDIO_DECIM].astype(np.float32)

        # 8. Stereo / mono decision based on pilot power
        if np.mean(pilot ** 2) > 1e-4:
            l = ((lpr + lmr) * 0.5).astype(np.float32)
            r = ((lpr - lmr) * 0.5).astype(np.float32)
        else:
            m = (lpr * 0.5).astype(np.float32)
            l = r = m

        # 9. De-emphasis
        l, self._de_l_zi = lfilter(self._de_b, self._de_a, l, zi=self._de_l_zi)
        r, self._de_r_zi = lfilter(self._de_b, self._de_a, r, zi=self._de_r_zi)

        return l.astype(np.float32), r.astype(np.float32), composite.astype(np.float32)
