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


_LIMITER_KNEE     = 0.85    # soft-knee starts here; converges to ±1.0 above
_SHELF_MAX_DEPTH  = 0.292   # (1 − 10^(−3/20)): blend fraction → -3 dB HF at noise_gate=0

# ITU-R BS.1770-4 K-weighting filter — two cascaded biquads, pre-computed for 48 kHz.
# Stage 1: head-acoustics pre-filter (high-shelf boost above ~1 kHz).
# Stage 2: RLB weighting (second-order highpass, de-weights LF where ears are less sensitive).
# Applying this to the audio signal before computing RMS gives a perceptual loudness
# estimate (LUFS-style) rather than flat-spectrum energy, so the AGC target is
# consistent across station formats regardless of spectral balance.
_K_WEIGHT_SOS = np.array([
    [1.53512485958697, -2.69169618940638, 1.19839281085285,
     1.0,              -1.69065929318241,  0.73248077421585],   # stage 1
    [1.0,              -2.0,               1.0,
     1.0,              -1.99004745483398,  0.99007225036498],   # stage 2
], dtype=np.float64)

def _soft_limit(x: np.ndarray) -> np.ndarray:
    """
    Soft-knee limiter: linear below the knee, tanh rolloff above.
    Avoids the harsh high-frequency harmonics that a hard np.clip creates
    on audio transients and high-pitched voices.
    """
    abs_x = np.abs(x)
    over  = abs_x > _LIMITER_KNEE
    x     = x.copy()
    k     = 1.0 - _LIMITER_KNEE
    x[over] = (np.sign(x[over])
               * (_LIMITER_KNEE + k * np.tanh((abs_x[over] - _LIMITER_KNEE) / k)))
    return x.clip(-1.0, 1.0)


class _SpectralSubtractor:
    """
    Single-channel online spectral noise subtraction using overlap-add STFT.

    When update_noise=True (silence / hiss detected by the AGC gate), the
    per-bin noise power spectral density is estimated with an EMA.  During
    music (update_noise=False) the stored estimate is subtracted from each
    FFT bin while preserving phase, with a hard spectral floor at floor_frac
    of the input amplitude to prevent musical-noise chirping artefacts.

    Uses sqrt-Hann analysis and synthesis windows; at 50 % overlap these
    satisfy the COLA constraint so unprocessed frames reconstruct exactly.
    Input/output lengths are always matched; the first call may contain a
    brief (n_fft - hop) sample latency padding of zeros (~10 ms at 48 kHz).
    """

    def __init__(self, n_fft: int = 1024, hop: int = 512,
                 over_sub: float = 0.6, floor_frac: float = 0.2,
                 noise_alpha: float = 0.9):
        self._n_fft       = n_fft
        self._hop         = hop
        self._over_sub    = over_sub      # subtract this fraction of noise estimate (< 1.0 reduces musical noise)
        self._floor_frac  = floor_frac    # floor as fraction of input amplitude (-14 dB at 0.2)
        self._noise_alpha = noise_alpha   # EMA smoothing of noise PSD estimate

        self._win         = np.sqrt(np.hanning(n_fft)).astype(np.float64)
        self._noise_psd   = np.zeros(n_fft // 2 + 1, dtype=np.float64)
        self._noise_ready = False

        self._in_q  = np.empty(0, dtype=np.float32)
        self._out_q = np.empty(0, dtype=np.float32)
        self._ola   = np.zeros(n_fft,    dtype=np.float64)

    def process(self, x: np.ndarray, update_noise: bool) -> np.ndarray:
        N = len(x)
        self._in_q = np.concatenate([self._in_q, x.astype(np.float32)])

        while len(self._in_q) >= self._n_fft:
            frame = self._in_q[:self._n_fft].astype(np.float64) * self._win
            X     = np.fft.rfft(frame)
            power = X.real ** 2 + X.imag ** 2

            if update_noise:
                if not self._noise_ready:
                    self._noise_psd   = power.copy()
                    self._noise_ready = True
                else:
                    self._noise_psd = (self._noise_alpha * self._noise_psd
                                       + (1.0 - self._noise_alpha) * power)
                X_out = X                     # pass through while estimating
            elif self._noise_ready:
                floor_p = (self._floor_frac ** 2) * power
                clean_p = np.maximum(power - self._over_sub * self._noise_psd,
                                     floor_p)
                X_out   = X * np.sqrt(clean_p / (power + 1e-30))
            else:
                X_out = X                     # no estimate yet, pass through

            frame_out       = np.fft.irfft(X_out, n=self._n_fft) * self._win
            self._ola      += frame_out
            ready           = self._ola[:self._hop].copy()
            self._ola       = np.roll(self._ola, -self._hop)
            self._ola[-self._hop:] = 0.0
            self._out_q     = np.concatenate([self._out_q,
                                               ready.astype(np.float32)])
            self._in_q      = self._in_q[self._hop:]

        if len(self._out_q) >= N:
            out         = self._out_q[:N].copy()
            self._out_q = self._out_q[N:]
        else:
            out         = np.concatenate([self._out_q,
                                           np.zeros(N - len(self._out_q),
                                                    dtype=np.float32)])
            self._out_q = np.empty(0, dtype=np.float32)
        return out


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

    def __init__(self, deemphasis_us: int = 75, stereo_mode: str = "auto"):
        # --- audio bandpass/lowpass filter coefficients (SOS) ---
        # Wide LPF at 15 kHz: full FM audio bandwidth for strong signals.
        # Narrow LPF at 8 kHz: used on weak signals to remove HF hiss while
        # preserving speech/music intelligibility.  The two paths are blended
        # by the stereo-blend factor so bandwidth narrows continuously as the
        # signal weakens (same technique used in hardware FM tuner ICs).
        self._lpr_sos        = butter(8, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        # Narrow path at 8 kHz.  Used at blend=0 on noisy stations to reduce
        # discriminator noise while preserving basic audio fidelity.  4 kHz
        # ("telephone") proved too restrictive — music became unintelligible.
        self._lpr_narrow_sos = butter(8,  8_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        self._pilot_sos      = butter(4, [17_000, 21_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_sos        = butter(4, [23_000, 53_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_lp_sos     = butter(8, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        # Narrow L-R path at 8 kHz — mirrors the L+R adaptive bandwidth logic.
        # On noisy signals the 8-15 kHz L-R content is mostly discriminator
        # noise, not program stereo.  Blending toward narrow when noise_gate is
        # low directly reduces the "ssss" hiss character without touching the
        # L+R mono channel or affecting clean stations (noise_gate ≈ 1).
        self._lmr_narr_sos   = butter(8,  8_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
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
        self._lmr_narr_zi    = _zero_zi(self._lmr_narr_sos)
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

        self._stereo_mode = stereo_mode   # "auto" | "force" | "mono"

        # --- noise-adaptive high-frequency shelf ---
        # A 9 kHz LPF whose output is blended with the passthrough by
        # (1 - noise_gate). Clean stations see no attenuation; noisy ones
        # get a smooth rolloff above 9 kHz (max -3 dB at noise_gate=0).
        # Separate smoothed noise_gate so the shelf works in all stereo modes,
        # including mono or force-stereo where pilot_gate may differ from noise_gate.
        self._shelf_sos         = butter(2, 9_000, 'lowpass', fs=_AUDIO_RATE, output='sos')
        self._shelf_l_zi        = _zero_zi(self._shelf_sos)
        self._shelf_r_zi        = _zero_zi(self._shelf_sos)
        self._noise_gate_smooth = 1.0   # start at full passthrough; converges on first block

        # --- blend smoothing state ---
        # Asymmetric time constants: fast attack (falling blend → protect ears
        # from noise burst) and slow release (rising blend → avoid flicker on
        # marginal signals).  At ~109 ms/block: α=0.3 → τ≈250 ms fall,
        # α=0.05 → τ≈1.5 s rise.
        # On the very first block we snap to blend_raw directly so there is no
        # slow ramp-up from 0 after every retune (e.g. when the user changes
        # gain, a new demodulator is created and blend would otherwise take
        # ~6 s to reach its steady-state value).
        self._blend_smooth = 0.0
        self._blend_init   = False   # True after first process() call

        # --- audio AGC state ---
        # Fast warmup for the first 20 blocks (~2 s) so the audio snaps to
        # target level immediately after tuning, then hands off to the slow
        # release (α=0.005, τ≈22 s) which prevents noise pumping at steady state.
        self._agc_gain   = 1.0
        self._agc_warmup = 20

        # --- spectral noise subtraction (one instance per channel) ---
        self._ss_l = _SpectralSubtractor()
        self._ss_r = _SpectralSubtractor()

        # --- K-weighting filter state (ITU-R BS.1770, per channel) ---
        self._kw_l_zi = _zero_zi(_K_WEIGHT_SOS)
        self._kw_r_zi = _zero_zi(_K_WEIGHT_SOS)

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

        # FM click blanking: a phase slip (cycle skip) produces a single
        # sample near ±DEMOD_RATE/MAX_DEV = ±3.2 normalized.  Legitimate
        # broadcast audio never exceeds ±1.0 (100% deviation); clipping
        # at ±1.5 removes the worst spikes before they survive the LPF
        # and appear as crackle in the audio output.
        composite = np.clip(composite, -1.5, 1.5)

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

        # 7. Coherent demod of DSB-SC L-R.  Two LPF paths computed here;
        #    they are blended by noise_gate after it is computed in step 8.
        lmr_demod                    = lmr_band * carrier38 * 2.0
        lmr_wide_full, self._lmr_lp_zi  = sosfilt(self._lmr_lp_sos,   lmr_demod, zi=self._lmr_lp_zi)
        lmr_narr_full, self._lmr_narr_zi = sosfilt(self._lmr_narr_sos, lmr_demod, zi=self._lmr_narr_zi)
        lmr_wide = lmr_wide_full[::_AUDIO_DECIM].astype(np.float32)
        lmr_narr = lmr_narr_full[::_AUDIO_DECIM].astype(np.float32)

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

        if self._stereo_mode == "mono":
            blend_raw = 0.0
        elif self._stereo_mode == "force":
            # Bypass noise gate; stereo whenever the station broadcasts a
            # pilot, regardless of signal quality.  User accepts more hiss.
            blend_raw = pilot_gate
        else:
            # iq_gate removed: noise_gate already measures actual SNR from
            # physics (65-90 kHz discriminator noise floor) and correctly
            # handles even sub-threshold signals (noise/pilot≫1 → gate=0).
            # iq_gate was a redundant proxy that created a cliff at iq_rms=0.10
            # where the gain controller crossing that threshold suddenly injected
            # L-R stereo noise into an otherwise clean mono signal.
            blend_raw = pilot_gate * noise_gate

        # Smooth blend with asymmetric time constants to prevent block-edge
        # clicks and flicker on marginal signals.
        if not self._blend_init:
            self._blend_smooth = blend_raw   # snap on first block; no 6-s ramp-up
            self._blend_init   = True
        else:
            alpha = 0.3 if blend_raw < self._blend_smooth else 0.05
            self._blend_smooth += alpha * (blend_raw - self._blend_smooth)
        blend = self._blend_smooth
        self.last_blend = blend

        # Adaptive L+R bandwidth: blend wide (15 kHz) and narrow (8 kHz)
        # proportionally to signal strength.
        lpr = (lpr_wide * blend + lpr_narrow * (1.0 - blend)).astype(np.float32)

        # Adaptive L-R bandwidth: same principle applied to the stereo channel.
        # At noise_gate=1 (clean): full 15 kHz L-R separation.
        # At noise_gate=0 (noisy): 8 kHz L-R — the 8-15 kHz L-R band on a
        # marginal signal is mostly discriminator noise, not program stereo.
        lmr = (lmr_wide * noise_gate + lmr_narr * (1.0 - noise_gate)).astype(np.float32)

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

        # 10a. Noise-adaptive high-frequency shelf.
        #      Blend passthrough with 9 kHz LPF output by (1 − noise_gate).
        #      At noise_gate=1 (clean): pure passthrough, 0 dB across band.
        #      At noise_gate=0.62 (WMSE): ≈ −1 dB above 15 kHz.
        #      At noise_gate=0 (unusable): ≈ −3 dB above 15 kHz.
        #      Fast drop (α=0.10) prevents an HF burst when the signal clears;
        #      slow rise (α=0.05) avoids flutter on marginal/fluctuating signals.
        alpha_shelf = 0.10 if noise_gate < self._noise_gate_smooth else 0.05
        self._noise_gate_smooth += alpha_shelf * (noise_gate - self._noise_gate_smooth)
        shelf_depth = (1.0 - self._noise_gate_smooth) * _SHELF_MAX_DEPTH
        lp_l, self._shelf_l_zi = sosfilt(self._shelf_sos, l32, zi=self._shelf_l_zi)
        lp_r, self._shelf_r_zi = sosfilt(self._shelf_sos, r32, zi=self._shelf_r_zi)
        l32 = ((1.0 - shelf_depth) * l32 + shelf_depth * lp_l).astype(np.float32)
        r32 = ((1.0 - shelf_depth) * r32 + shelf_depth * lp_r).astype(np.float32)

        # 10b. Spectral noise subtraction.
        #      Compute pre-subtraction RMS to drive both the denoiser gate and
        #      the AGC gate below.  During silence (rms ≤ _AGC_GATE) the per-bin
        #      noise PSD is updated; during music it is subtracted per-bin with a
        #      spectral floor at 10 % amplitude to prevent musical-noise artefacts.
        _AGC_GATE = 0.025
        rms       = float(np.sqrt(np.mean(l32 ** 2 + r32 ** 2) / 2)) + 1e-10
        in_noise  = rms <= _AGC_GATE
        l32       = self._ss_l.process(l32, in_noise)
        r32       = self._ss_r.process(r32, in_noise)

        # 10c. K-weighted loudness measurement (ITU-R BS.1770).
        #      Filter both channels through the two-stage K-weighting SOS and
        #      compute RMS of the result.  K-weighted RMS ≈ perceived loudness:
        #      bright stations and bass-heavy stations that would measure the same
        #      on a flat-RMS meter now correctly measure as equally loud.  The
        #      gate and spectral-subtraction gate still use broadband rms (above)
        #      since the K-weighting HF boost would make hiss appear louder and
        #      could prevent the silence gate from firing.
        kw_l, self._kw_l_zi = sosfilt(_K_WEIGHT_SOS, l32, zi=self._kw_l_zi)
        kw_r, self._kw_r_zi = sosfilt(_K_WEIGHT_SOS, r32, zi=self._kw_r_zi)
        rms_k = float(np.sqrt(np.mean(kw_l ** 2 + kw_r ** 2) / 2)) + 1e-10

        # 10d. Asymmetric audio AGC + soft-knee limiter.
        #     target_gain drives K-weighted loudness to 0.12 rather than flat RMS,
        #     so the AGC converges on equal perceived loudness across all stations.
        #     Time constants and gate are unchanged from before.
        target_gain = 0.12 / rms_k
        if rms > _AGC_GATE:
            if self._agc_warmup > 0:
                self._agc_warmup -= 1
                alpha_agc = 0.3   # fast convergence for first ~2 s after tuning
            else:
                alpha_agc = 0.3 if target_gain < self._agc_gain else 0.005
            self._agc_gain += alpha_agc * (target_gain - self._agc_gain)
            self._agc_gain = float(np.clip(self._agc_gain, 0.1, 10.0))
        l32 = _soft_limit((l32 * self._agc_gain).astype(np.float64)).astype(np.float32)
        r32 = _soft_limit((r32 * self._agc_gain).astype(np.float64)).astype(np.float32)

        self.last_audio_rms = float(np.sqrt(np.mean(l32 ** 2 + r32 ** 2) / 2))
        return l32, r32, composite.astype(np.float32)
