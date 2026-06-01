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
    last_noise_rms:    float = 0.0   # discriminator noise floor (65-90 kHz band)
    last_blend:        float = 0.0   # stereo blend factor 0-1
    last_audio_rms:    float = 0.0   # decoded output level

    def __init__(self, deemphasis_us: int = 75):
        # --- audio bandpass/lowpass filter coefficients (SOS) ---
        # Wide LPF at 15 kHz: full FM audio bandwidth for strong signals.
        # Narrow LPF at 8 kHz: used on weak signals to remove HF hiss while
        # preserving speech/music intelligibility.  The two paths are blended
        # by the stereo-blend factor so bandwidth narrows continuously as the
        # signal weakens (same technique used in hardware FM tuner ICs).
        self._lpr_sos        = butter(8, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        self._lpr_narrow_sos = butter(8,  8_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        self._pilot_sos      = butter(4, [17_000, 21_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_sos        = butter(4, [23_000, 53_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_lp_sos     = butter(8, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        # Above FM program content (L+R 0-15k, pilot 19k, L-R 23-53k, RDS 57k)
        # and below Nyquist (120k): this band contains only discriminator noise.
        # Its RMS is a direct measure of FM SNR and drives the noise gate.
        self._noise_sos      = butter(4, [65_000, 90_000],    'bandpass', fs=_DEMOD_RATE, output='sos')

        # --- per-block filter states ---
        self._lpr_zi         = _zero_zi(self._lpr_sos)
        self._lpr_narrow_zi  = _zero_zi(self._lpr_narrow_sos)
        self._pilot_zi       = _zero_zi(self._pilot_sos)
        self._lmr_zi         = _zero_zi(self._lmr_sos)
        self._lmr_lp_zi      = _zero_zi(self._lmr_lp_sos)
        self._noise_zi       = _zero_zi(self._noise_sos)

        # --- de-emphasis IIR (first-order) ---
        dt    = 1.0 / _AUDIO_RATE
        tau   = deemphasis_us * 1e-6
        alpha = dt / (tau + dt)
        self._de_b     = np.array([alpha],          dtype=np.float64)
        self._de_a     = np.array([1.0, -(1-alpha)], dtype=np.float64)
        self._de_l_zi  = np.zeros(1)
        self._de_r_zi  = np.zeros(1)

        # --- DC blocker (post de-emphasis) ---
        # The coherent L-R demodulator (pilot² → carrier38) can introduce a
        # small DC bias when the carrier normalization is imperfect.  De-emphasis
        # passes DC at unity gain so nothing else removes it; a 5 Hz highpass
        # removes the offset without touching any audible FM content (≥ 30 Hz).
        self._dc_sos   = butter(2, 5, 'highpass', fs=_AUDIO_RATE, output='sos')
        self._dc_l_zi  = _zero_zi(self._dc_sos)
        self._dc_r_zi  = _zero_zi(self._dc_sos)

        # --- blend smoothing state ---
        # Asymmetric time constants: fast attack (falling blend → protect ears
        # from noise burst) and slow release (rising blend → avoid flicker on
        # marginal signals).  At ~109 ms/block: α=0.3 → τ≈250 ms fall,
        # α=0.05 → τ≈1.5 s rise.
        self._blend_smooth = 0.0

        # --- audio AGC state ---
        # Very slow update (α=0.02 → τ≈5 s) prevents pumping artefacts.
        # Gain clamped to 0.1–10× to avoid runaway on silence or clipping.
        self._agc_gain = 1.0

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

        # 3. L+R: two parallel paths — wide (15 kHz) and narrow (8 kHz).
        #    The narrow path removes HF hiss on weak signals; they are blended
        #    below once the blend factor has been computed.
        lpr_full,        self._lpr_zi        = sosfilt(self._lpr_sos,        composite, zi=self._lpr_zi)
        lpr_narrow_full, self._lpr_narrow_zi = sosfilt(self._lpr_narrow_sos, composite, zi=self._lpr_narrow_zi)
        lpr_wide   = lpr_full[::_AUDIO_DECIM].astype(np.float32)
        lpr_narrow = lpr_narrow_full[::_AUDIO_DECIM].astype(np.float32)

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

        # 8. Stereo blend based on pilot RMS and raw IQ signal strength.
        #
        #    Typical pilot RMS on a good FM signal ≈ 0.07 (pilot is 10% of
        #    total deviation; RMS of sine = A/√2).
        #
        #    Empirical thresholds from three-station test:
        #      91.7  IQ 0.062 → iq_gate≈0.08  → blend≈ 8% (near-mono)
        #      88.9  IQ 0.088 → iq_gate≈0.25  → blend≈18%
        #      102.9 IQ 0.282 → iq_gate≈1.0   → blend≈77% (full stereo)
        pilot_rms = float(np.sqrt(np.mean(pilot ** 2)))
        iq_rms    = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
        self.last_pilot_rms     = pilot_rms
        self.last_iq_rms        = iq_rms
        self.last_composite_rms = float(np.sqrt(np.mean(composite ** 2)))

        # Measure discriminator noise floor in the 65-90 kHz band (no FM
        # program content there).  Normalize against pilot so the gate is
        # self-calibrating regardless of RTL-SDR gain or signal level.
        # noise_rms/pilot_rms ≈ 0 on a clean signal; ≈ 1+ on a noisy one.
        # _NOISE_RATIO_SCALE sets the ratio at which the gate fully closes
        # (tune this constant once real noise_rms values are observed).
        _NOISE_RATIO_SCALE = 1.5
        noise_band, self._noise_zi = sosfilt(self._noise_sos, composite, zi=self._noise_zi)
        noise_rms = float(np.sqrt(np.mean(noise_band ** 2)))
        self.last_noise_rms = noise_rms
        noise_gate = float(np.clip(
            1.0 - (noise_rms / (pilot_rms + 1e-6)) / _NOISE_RATIO_SCALE,
            0.0, 1.0,
        ))

        pilot_gate = float(np.clip((pilot_rms - 0.02) / 0.06, 0.0, 1.0))
        iq_gate    = float(np.clip((iq_rms    - 0.05) / 0.15, 0.0, 1.0))
        blend_raw  = pilot_gate * iq_gate * noise_gate

        # Smooth blend with asymmetric time constants to prevent block-edge
        # clicks and flicker on marginal signals.
        alpha = 0.3 if blend_raw < self._blend_smooth else 0.05
        self._blend_smooth += alpha * (blend_raw - self._blend_smooth)
        blend = self._blend_smooth
        self.last_blend = blend

        # Adaptive audio bandwidth: blend wide (15 kHz) and narrow (8 kHz)
        # L+R paths proportionally to signal strength.  On weak signals the
        # high-frequency path carries mainly discriminator noise; this removes
        # it while preserving speech/music intelligibility.
        lpr = (lpr_wide * blend + lpr_narrow * (1.0 - blend)).astype(np.float32)

        l = (lpr + lmr * blend).astype(np.float32)
        r = (lpr - lmr * blend).astype(np.float32)

        # 9. De-emphasis
        l, self._de_l_zi = lfilter(self._de_b, self._de_a, l, zi=self._de_l_zi)
        r, self._de_r_zi = lfilter(self._de_b, self._de_a, r, zi=self._de_r_zi)

        # 9b. DC blocker — remove any carrier-induced DC bias before encoding
        l, self._dc_l_zi = sosfilt(self._dc_sos, l, zi=self._dc_l_zi)
        r, self._dc_r_zi = sosfilt(self._dc_sos, r, zi=self._dc_r_zi)

        l32 = l.astype(np.float32)
        r32 = r.astype(np.float32)

        # 10. Slow audio AGC: normalise perceived loudness across stations.
        #     Target RMS is scaled by the smoothed blend factor so that on a
        #     noisy signal (blend→0) the AGC settles at a quiet background
        #     level rather than amplifying discriminator noise to full volume.
        #     At blend=0: target=0.05 (−26 dBFS, near-silent).
        #     At blend=1: target=0.25 (−12 dBFS, normal level).
        rms = float(np.sqrt(np.mean(l32 ** 2 + r32 ** 2) / 2)) + 1e-10
        agc_target = 0.05 + 0.20 * blend
        self._agc_gain += 0.02 * (agc_target / rms - self._agc_gain)
        self._agc_gain = float(np.clip(self._agc_gain, 0.1, 10.0))
        l32 = np.clip(l32 * self._agc_gain, -1.0, 1.0).astype(np.float32)
        r32 = np.clip(r32 * self._agc_gain, -1.0, 1.0).astype(np.float32)

        self.last_audio_rms = float(np.sqrt(np.mean(l32 ** 2 + r32 ** 2) / 2))
        return l32, r32, composite.astype(np.float32)
