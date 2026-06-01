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

# 1.2 MHz is sufficient for the FM composite (L+R to 15 kHz, L-R to 53 kHz,
# RDS at 57 kHz — well below the 600 kHz Nyquist limit).  Using 2.4 MHz
# doubled the samples-per-block without any audio benefit, and caused the
# pyrtlsdr read queue to overflow ("extra callback data lost") at ~5 Hz,
# which was the source of the digital cutting/static artefacts.
_SAMPLE_RATE = 1_200_000
_DEMOD_RATE  = 240_000
_AUDIO_RATE  = 48_000
_CHAN_DECIM  = _SAMPLE_RATE // _DEMOD_RATE   # 5
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

    # Per-block metrics — read by pipeline.py for the diagnostics panel.
    last_pilot_rms:    float = 0.0
    last_iq_rms:       float = 0.0   # raw ADC signal power
    last_composite_rms: float = 0.0  # FM discriminator output
    last_blend:        float = 0.0   # stereo blend factor 0-1
    last_audio_rms:    float = 0.0   # decoded output level

    def __init__(self, deemphasis_us: int = 75):
        # --- audio bandpass/lowpass filter coefficients (SOS) ---
        # 8th-order LPF at 15 kHz: passes the full FM audio band (-3 dB at
        # 15 kHz) while providing ~26 dB attenuation at 19 kHz to reject
        # pilot bleed.  The previous 6th-order at 14.5 kHz was attenuating
        # the top octave slightly, making the audio sound dull.
        self._lpr_sos     = butter(8, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        self._pilot_sos   = butter(4, [17_000, 21_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_sos     = butter(4, [23_000, 53_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_lp_sos  = butter(8, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')

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
        # 1. Decimate ×5 (1.2 MHz → 240 kHz).  resample_poly uses SIMD-optimised
        #    upfirdn internally and is significantly faster than lfilter for
        #    large arrays.  The minor block-edge transient it introduces is
        #    inaudible compared to the queue-overflow dropouts that the slower
        #    lfilter approach was causing.
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

        # 6. Generate 38 kHz carrier via Hilbert squaring of pilot.
        #    hilbert(pilot) ≈ A·e^{jφ}; squaring → A²·e^{j2φ} at 38 kHz.
        #    Normalise to unit amplitude so pilot level doesn't AM-modulate
        #    the L-R demodulation product (which caused background static).
        pilot_a   = hilbert(pilot).astype(np.complex64)
        c38_raw   = (pilot_a ** 2)
        carrier38 = (c38_raw / (np.abs(c38_raw) + 1e-10)).real.astype(np.float64)

        # 7. Coherent demod of DSB-SC L-R, LPF, decimate
        lmr_demod              = lmr_band * carrier38 * 2.0
        lmr_full, self._lmr_lp_zi = sosfilt(self._lmr_lp_sos, lmr_demod, zi=self._lmr_lp_zi)
        lmr = lmr_full[::_AUDIO_DECIM].astype(np.float32)

        # 8. Stereo blend based on pilot RMS.
        #    Hard switching causes an abrupt noise burst on weak signals.
        #    Soft blend smoothly fades L-R contribution as pilot weakens.
        #
        #    Typical pilot RMS on a good FM signal ≈ 0.07 (pilot is 10% of
        #    total deviation; RMS of sine = A/√2).
        #
        #    blend = 0 (mono) below pilot_rms 0.02, = 1 (full stereo) above 0.08.
        pilot_rms = float(np.sqrt(np.mean(pilot ** 2)))
        iq_rms    = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
        self.last_pilot_rms    = pilot_rms
        self.last_iq_rms       = iq_rms
        self.last_composite_rms = float(np.sqrt(np.mean(composite ** 2)))

        # Stereo blend gated by BOTH pilot presence AND raw signal strength.
        #
        # The FM discriminator normalises by deviation, so pilot_rms is roughly
        # the same (~0.07) on a strong or weak signal — it only tells us the
        # station *broadcasts* stereo, not whether the signal is clean enough
        # to demodulate L-R without adding noise.
        #
        # iq_rms reflects the actual received signal power at the ADC.  Gating
        # on it prevents 100% stereo blend on weak signals where the L-R
        # subcarrier SNR is poor, which was the source of the 91.7 hiss.
        #
        # Empirical thresholds from three-station test:
        #   91.7  IQ 0.062 → iq_gate≈0.08  → blend≈ 8% (near-mono, quiet)
        #   88.9  IQ 0.088 → iq_gate≈0.25  → blend≈18%
        #   102.9 IQ 0.282 → iq_gate≈1.0   → blend≈77% (full stereo)
        pilot_gate = float(np.clip((pilot_rms - 0.02) / 0.06, 0.0, 1.0))
        iq_gate    = float(np.clip((iq_rms    - 0.05) / 0.15, 0.0, 1.0))
        blend      = pilot_gate * iq_gate
        self.last_blend = blend

        l = (lpr + lmr * blend).astype(np.float32)
        r = (lpr - lmr * blend).astype(np.float32)

        # 9. De-emphasis
        l, self._de_l_zi = lfilter(self._de_b, self._de_a, l, zi=self._de_l_zi)
        r, self._de_r_zi = lfilter(self._de_b, self._de_a, r, zi=self._de_r_zi)

        l32 = l.astype(np.float32)
        r32 = r.astype(np.float32)
        self.last_audio_rms = float(np.sqrt(np.mean(l32 ** 2 + r32 ** 2) / 2))
        return l32, r32, composite.astype(np.float32)
