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


_LIMITER_KNEE        = 0.85    # soft-knee starts here; converges to ±1.0 above
_STEREO_RESTORE_MAX  = 1.2    # max side-channel boost at blend=0; scales linearly with (1-blend)

# Minimum Statistics noise floor estimation.
# A circular buffer of _MINSTAT_FRAMES per-bin power spectra is maintained across
# every STFT frame (silence or not).  The per-bin minimum over that window
# approximates the noise floor; _MINSTAT_BIAS compensates for the statistical
# tendency of the minimum to underestimate the true noise floor.
# 128 hops × 512 samples / 48 kHz ≈ 1.4 s of history — long enough to see the
# noise floor between notes/words, short enough to track slowly drifting SNR.
#
# The np.min(axis=0) scan is a cache-unfriendly column-wise reduction over a
# row-major array; doing it every STFT frame (~94 Hz, 2 channels) adds
# measurable overhead on a Pi 4 and causes USB callback overflows.
# _MINSTAT_UPDATE_EVERY gates the scan to every N frames (~12 Hz); the noise
# floor changes on timescales of seconds so 12 Hz is more than sufficient.
_MINSTAT_FRAMES       = 128
_MINSTAT_BIAS         = 1.66  # geometric mean of 1.25 and 2.0; Martin (2001) recommends 1.5-2.0
_MINSTAT_UPDATE_EVERY = 8

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
    Single-channel online spectral noise reduction using overlap-add STFT.

    Noise floor — three complementary estimators:
      MinStat — per-bin minimum power over a _MINSTAT_FRAMES circular buffer,
                updated every STFT frame.  Primary estimator; works even on
                stations with no silence pauses.
      EMA     — updated only during AGC-detected silence; converges faster
                on stations that have pauses; used as a refinement.
      Physics — derived from the discriminator's 65-90 kHz noise-floor RMS,
                which is a clean physical measurement immune to musical-content
                contamination of the MinStat buffer.  Self-calibrated during
                silence frames.  Acts as a floor: noise_est never drops below
                what the discriminator says the noise actually is.
    Estimate = max(min(MinStat, EMA), physics_floor).

    Gain model: Ephraim-Malah (1984) decision-directed Wiener filter.
      γ[k]  = power / noise_est           (a posteriori SNR)
      ξ[k]  = α·G[k-1]²·γ[k-1]  +  (1-α)·max(γ[k]-1, 0)   (a priori SNR)
      G[k]  = ξ[k] / (ξ[k]+1)            (Wiener gain ∈ [0,1))
    The decision-directed smoother (α=0.92, τ≈250 ms) gives smooth gain
    trajectories that eliminate both musical-noise artefacts and the
    sibilant-onset static that a hard spectral floor produces.  Where
    _sub_mask=0 the gain is clamped to 1 (pass-through).

    A 3-bin triangular frequency smoother is applied to the gain output.

    Intended to run pre-de-emphasis: the pre-emphasized FM signal has
    +6 to +17 dB of HF boost relative to the flat discriminator noise,
    giving reliable a priori SNR estimates across the full 0-15 kHz band.

    Uses sqrt-Hann analysis and synthesis windows; at 50 % overlap these
    satisfy the COLA constraint so unprocessed frames reconstruct exactly.
    Input/output lengths are always matched; the first call may contain a
    brief (n_fft - hop) sample latency padding of zeros (~10 ms at 48 kHz).
    """

    def __init__(self, n_fft: int = 1024, hop: int = 512,
                 alpha_dd: float = 0.92,
                 noise_alpha: float = 0.9):
        self._n_fft       = n_fft
        self._hop         = hop
        self._alpha_dd    = alpha_dd     # decision-directed SNR smoother; τ ≈ 250 ms at 48 kHz/512-hop
        self._noise_alpha = noise_alpha  # EMA smoothing for silence-gated noise PSD

        self._win         = np.sqrt(np.hanning(n_fft)).astype(np.float64)
        self._noise_psd   = np.zeros(n_fft // 2 + 1, dtype=np.float64)
        self._noise_ready = False

        # MinStat circular buffer: shape (_MINSTAT_FRAMES, n_bins).
        # Initialised to inf so that np.min over unfilled slots never beats real data.
        _n_bins              = n_fft // 2 + 1
        self._ms_buf         = np.full((_MINSTAT_FRAMES, _n_bins), np.inf, dtype=np.float64)
        self._ms_idx         = 0    # next write slot (wraps)
        self._ms_fill        = 0    # number of valid frames written (capped at _MINSTAT_FRAMES)
        # Cached min result; recomputed every _MINSTAT_UPDATE_EVERY frames.
        # Initialise to _MINSTAT_UPDATE_EVERY-1 so the first frame triggers a recompute.
        self._ms_frame_ctr   = _MINSTAT_UPDATE_EVERY - 1
        self._ms_min_cache   = np.zeros(_n_bins, dtype=np.float64)

        # Decision-directed Wiener state — initialised to 1 (full pass-through).
        self._prev_gain  = np.ones(_n_bins, dtype=np.float64)
        self._prev_gamma = np.ones(_n_bins, dtype=np.float64)

        # Physics-based noise floor state.
        # _phys_scale converts noise_rms² (discriminator 65-90 kHz measurement)
        # to per-STFT-bin power units at the Wiener input.  Analytically ≈ 307
        # (PSD × 15 kHz effective bandwidth × 512 STFT integration); refined in
        # place during silence frames via a very slow EMA so any pipeline gain
        # drift is absorbed automatically.
        self._fm_bins    = int(15_000 * n_fft / 48_000)   # 320 bins for n_fft=1024
        self._phys_scale = 307.0
        self._phys_alpha = 0.005   # τ ≈ 200 silence frames before scale is reliable

        # Subtraction mask: flat 1.0 across 0-15 kHz (FM audio bandwidth),
        # linear taper to 0 at 16 kHz.  Bins above the FM bandwidth have no
        # programme content; forcing gain=1 there avoids erratic Wiener
        # behaviour in those bins.
        _freqs            = np.arange(_n_bins) * (48_000.0 / n_fft)
        self._sub_mask    = np.clip((16_000.0 - _freqs) / 1_000.0,
                                    0.0, 1.0).astype(np.float64)

        self._in_q  = np.empty(0, dtype=np.float32)
        self._out_q = np.empty(0, dtype=np.float32)
        self._ola   = np.zeros(n_fft,    dtype=np.float64)

    def process(self, x: np.ndarray, update_noise: bool,
                noise_rms: float = 0.0) -> np.ndarray:
        N = len(x)
        self._in_q = np.concatenate([self._in_q, x.astype(np.float32)])

        while len(self._in_q) >= self._n_fft:
            frame = self._in_q[:self._n_fft].astype(np.float64) * self._win
            X     = np.fft.rfft(frame)
            power = X.real ** 2 + X.imag ** 2

            # MinStat update — every frame, silence or not.
            self._ms_buf[self._ms_idx] = power
            self._ms_idx  = (self._ms_idx + 1) % _MINSTAT_FRAMES
            self._ms_fill = min(self._ms_fill + 1, _MINSTAT_FRAMES)
            # Gate the expensive axis=0 scan to every _MINSTAT_UPDATE_EVERY frames.
            self._ms_frame_ctr = (self._ms_frame_ctr + 1) % _MINSTAT_UPDATE_EVERY
            if self._ms_frame_ctr == 0:
                self._ms_min_cache = (np.min(self._ms_buf[:self._ms_fill], axis=0)
                                      * _MINSTAT_BIAS)
            min_psd = self._ms_min_cache

            # EMA update during silence — fast-convergence helper; non-critical
            # now that MinStat provides a continuous estimate.
            if update_noise:
                if not self._noise_ready:
                    self._noise_psd   = power.copy()
                    self._noise_ready = True
                else:
                    self._noise_psd = (self._noise_alpha * self._noise_psd
                                       + (1.0 - self._noise_alpha) * power)

            # Primary noise estimate: MinStat minimum.  When the EMA has seen at
            # least one silence frame its estimate is tighter (it tracks gaps in
            # the programme); take the lower of the two so we never over-subtract.
            if self._noise_ready:
                noise_est = np.minimum(min_psd, self._noise_psd)
            else:
                noise_est = min_psd

            # Physics floor — from the discriminator's 65-90 kHz noise-floor
            # measurement.  During dense music the MinStat minimum can be
            # contaminated by programme content that occupies the bin in every
            # frame of the 1.4-second buffer; the physics measurement is immune
            # to that.  Calibrate the noise_rms² → per-bin-power scale factor
            # during silence frames; apply it as a floor at all other times.
            if noise_rms > 0.0:
                if update_noise:
                    _mean_pwr        = float(power[:self._fm_bins].mean())
                    _target_scale    = _mean_pwr / (noise_rms ** 2 + 1e-30)
                    self._phys_scale += self._phys_alpha * (_target_scale - self._phys_scale)
                noise_est = np.maximum(noise_est, noise_rms ** 2 * self._phys_scale)

            # Ephraim-Malah decision-directed Wiener gain.
            # γ = a posteriori SNR; ξ = a priori SNR tracked via the previous
            # frame's gain-adjusted SNR.  Wiener gain G = ξ/(ξ+1) ∈ [0,1).
            gamma    = power / (noise_est + 1e-30)
            xi       = (self._alpha_dd * self._prev_gain ** 2 * self._prev_gamma
                        + (1.0 - self._alpha_dd) * np.maximum(gamma - 1.0, 0.0))
            xi       = np.maximum(xi, 1e-10)
            gain     = xi / (xi + 1.0)

            # 3-bin frequency smoother — catches any residual bin-by-bin
            # chatter that the temporal decision-directed smoother misses.
            g_s       = gain.copy()
            g_s[1:-1] = 0.25 * gain[:-2] + 0.5 * gain[1:-1] + 0.25 * gain[2:]

            # Where mask=0 (above FM audio bandwidth) force gain to 1.
            X_out = X * (self._sub_mask * g_s + (1.0 - self._sub_mask))

            # Update decision-directed state with unsmoothed gain; using the
            # smoothed version would flatten the temporal response.
            self._prev_gain  = gain
            self._prev_gamma = gamma

            frame_out                          = np.fft.irfft(X_out, n=self._n_fft) * self._win
            self._ola                         += frame_out
            ready                              = self._ola[:self._hop].copy()
            self._ola[:self._n_fft-self._hop]  = self._ola[self._hop:]
            self._ola[self._n_fft-self._hop:]  = 0.0
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

    # Per-block metrics — read by pipeline.py for signal strength and stereo detection.
    last_pilot_rms:    float = 0.0
    last_iq_rms:       float = 0.0   # raw ADC signal power (RF level)
    last_composite_rms: float = 0.0  # FM discriminator output
    last_noise_rms:    float = 0.0   # discriminator noise floor (65-90 kHz band)
    last_blend:        float = 0.0   # stereo blend factor 0-1
    last_audio_rms:    float = 0.0   # decoded output level

    def __init__(self, deemphasis_us: int = 75, stereo_mode: str = "auto"):
        # --- audio bandpass/lowpass filter coefficients (SOS) ---
        # Single L+R path at full 15 kHz FM bandwidth.  A blended narrow path
        # (8 kHz) was previously used to reduce HF hiss on weak signals; the
        # Wiener filter now handles that pre-de-emphasis with better SNR estimates,
        # so the narrow path was discarding real programme content unnecessarily.
        self._lpr_sos        = butter(8, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        self._pilot_sos      = butter(4, [17_000, 21_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_sos        = butter(4, [23_000, 53_000],    'bandpass', fs=_DEMOD_RATE, output='sos')
        self._lmr_lp_sos     = butter(8, 15_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        # Narrow L-R path at 11 kHz (was 8 kHz).  Blended toward this on weak
        # signals to prevent phantom stereo images and the "swishing" artefact
        # caused by discriminator noise in the 11-15 kHz L-R band.  The Wiener
        # filter handles 8-11 kHz L-R noise; the top octave (11-15 kHz) is the
        # residual where stereo content is absent on marginal signals.
        self._lmr_narr_sos   = butter(8, 11_000,              'lowpass',  fs=_DEMOD_RATE, output='sos')
        # Above FM program content (L+R 0-15k, pilot 19k, L-R 23-53k, RDS 57k)
        # and below Nyquist (120k): this band contains only discriminator noise.
        # Its RMS is a direct measure of FM SNR and drives the noise gate.
        self._noise_sos      = butter(4, [65_000, 90_000],    'bandpass', fs=_DEMOD_RATE, output='sos')

        # --- per-block filter states ---
        self._lpr_zi         = _zero_zi(self._lpr_sos)
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

        # --- stereo width restoration filter ---
        # Bandpass the side channel to 300–3500 Hz before boosting.  HF L-R
        # noise (above ~4 kHz) stays attenuated; the musically important midrange
        # stereo content (voices, instruments) is selectively recovered.
        self._swid_sos = butter(4, [300, 3_500], 'bandpass', fs=_AUDIO_RATE, output='sos')
        self._swid_zi  = _zero_zi(self._swid_sos)

        # --- spectral noise subtraction (one instance per channel) ---
        self._ss_l = _SpectralSubtractor()
        self._ss_r = _SpectralSubtractor()

        # --- K-weighting filter state (ITU-R BS.1770, per channel) ---
        self._kw_l_zi      = _zero_zi(_K_WEIGHT_SOS)
        self._kw_r_zi      = _zero_zi(_K_WEIGHT_SOS)
        self._rms_k_smooth = 0.12   # smoothed K-weighted RMS; init at target level

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

        # FM click blanking — two stages:
        #
        # Stage 1: hard clip at ±1.5 catches single-sample phase slips
        # (cycle skips) which can reach ±3.2 normalised.
        composite = np.clip(composite, -1.5, 1.5)
        #
        # Stage 2: multi-sample burst interpolation.  A hard clip turns a
        # 2–5 sample burst into a rectangular pulse that rings through the
        # 15 kHz LPF and becomes an audible click (~1.5 dB above typical
        # audio level).  Anything above ±1.1 (146 % of max FM deviation)
        # is noise/phase-slip — nothing legitimate exceeds that.  The mask
        # is dilated by 1 sample on each side to absorb the burst slope;
        # flagged samples are replaced with linear interpolation over clean
        # neighbours.  The `any()` guard makes this a no-op on clean blocks.
        _ck        = np.abs(composite) > 1.1
        _ck[1:]   |= _ck[:-1]
        _ck[:-1]  |= _ck[1:]
        if _ck.any():
            _xi            = np.where(~_ck)[0]
            composite[_ck] = np.interp(np.where(_ck)[0], _xi, composite[_xi])

        # 3. L+R: single 15 kHz path — full FM audio bandwidth.
        #    HF noise on weak signals is handled by the Wiener filter (step 9).
        lpr_full, self._lpr_zi = sosfilt(self._lpr_sos, composite, zi=self._lpr_zi)
        lpr_wide = lpr_full[::_AUDIO_DECIM].astype(np.float32)

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

        lpr = lpr_wide

        # Adaptive L-R bandwidth: blend toward 11 kHz on weak signals.
        # At noise_gate=1 (clean): full 15 kHz stereo separation.
        # At noise_gate=0 (noisy): 11 kHz — prevents phantom stereo images and
        # the "swishing" artefact from discriminator noise in the 11-15 kHz L-R
        # band.  The Wiener filter handles 8-11 kHz L-R noise; only the top
        # octave where real stereo content is absent is blended out here.
        lmr = (lmr_wide * noise_gate + lmr_narr * (1.0 - noise_gate)).astype(np.float32)

        l = (lpr + lmr * blend).astype(np.float32)
        r = (lpr - lmr * blend).astype(np.float32)

        # 9. Spectral noise reduction — pre-de-emphasis.
        #    FM stations apply 75 µs pre-emphasis before transmission, boosting
        #    the signal by +6 dB at 4 kHz, +10 dB at 8 kHz, +17 dB at 15 kHz
        #    relative to the discriminator noise floor, which is spectrally flat
        #    (white).  Operating here instead of post-de-emphasis gives the
        #    Wiener filter a much better per-bin SNR estimate across the full
        #    0-15 kHz audio band — particularly at the HF end where sibilants,
        #    hi-hats, and string transients live.
        _AGC_GATE = 0.025
        l32       = l.astype(np.float32)
        r32       = r.astype(np.float32)
        rms_pre   = float(np.sqrt(np.mean(l32 ** 2 + r32 ** 2) / 2)) + 1e-10
        in_noise  = rms_pre <= _AGC_GATE
        l32       = self._ss_l.process(l32, in_noise, noise_rms)
        r32       = self._ss_r.process(r32, in_noise, noise_rms)

        # 9b. De-emphasis (75 µs)
        l, self._de_l_zi = lfilter(self._de_b, self._de_a, l32, zi=self._de_l_zi)
        r, self._de_r_zi = lfilter(self._de_b, self._de_a, r32, zi=self._de_r_zi)

        # 9c. DC blocker — remove any carrier-induced DC bias before encoding
        l, self._dc_l_zi = sosfilt(self._dc_sos, l, zi=self._dc_l_zi)
        r, self._dc_r_zi = sosfilt(self._dc_sos, r, zi=self._dc_r_zi)

        l32 = l.astype(np.float32)
        r32 = r.astype(np.float32)

        # 10b. Signal level measurement for the AGC gate below.
        #      Broadband RMS is used (not K-weighted) because the K-weighting
        #      HF boost would make hiss appear louder and prevent the gate firing.
        rms = float(np.sqrt(np.mean(l32 ** 2 + r32 ** 2) / 2)) + 1e-10

        # 10b.5  Stereo width restoration.
        #        The blend gate attenuates L-R uniformly to suppress discriminator
        #        noise, but HF noise (above ~4 kHz) is what forced the blend down —
        #        the midrange L-R content (300–3500 Hz) has acceptable SNR and
        #        contains most of the musical stereo information (voices, guitars,
        #        synths).  Boost the bandpassed side proportionally to (1-blend)
        #        to recover that width without amplifying the HF noise floor.
        #        At blend=1 (full stereo): restore=0, pass-through.
        #        At blend=0.42 (WMSE): 300-3500 Hz side is boosted ~1.7×.
        #        At blend=0 (full mono): side=0, restoration is a no-op.
        mid             = (l32 + r32) * 0.5
        side            = (l32 - r32) * 0.5
        side_bp, self._swid_zi = sosfilt(self._swid_sos, side, zi=self._swid_zi)
        restore         = (1.0 - blend) * _STEREO_RESTORE_MAX
        enhanced_side   = (side + restore * side_bp).astype(np.float32)
        l32             = (mid + enhanced_side).astype(np.float32)
        r32             = (mid - enhanced_side).astype(np.float32)

        # 10c. K-weighted loudness measurement (ITU-R BS.1770).
        #      Filter both channels through the two-stage K-weighting SOS and
        #      compute RMS of the result.  K-weighted RMS ≈ perceived loudness:
        #      bright stations and bass-heavy stations that would measure the same
        #      on a flat-RMS meter now correctly measure as equally loud.  The
        #      The AGC gate uses broadband rms (step 10b) since the K-weighting
        #      HF boost would make hiss appear louder and prevent it from firing.
        kw_l, self._kw_l_zi = sosfilt(_K_WEIGHT_SOS, l32, zi=self._kw_l_zi)
        kw_r, self._kw_r_zi = sosfilt(_K_WEIGHT_SOS, r32, zi=self._kw_r_zi)
        rms_k = float(np.sqrt(np.mean(kw_l ** 2 + kw_r ** 2) / 2)) + 1e-10

        # 10d. Asymmetric audio AGC + soft-knee limiter.
        #      rms_k is smoothed over ~2 s (α=0.05, τ≈20 blocks) before computing
        #      target_gain.  Raw per-block K-weighted RMS varies more than broadband
        #      RMS because the K-weighting pre-filter boosts 2–8 kHz — exactly
        #      where sibilants and voice transients concentrate.  Without smoothing,
        #      the fast attack chases individual phonemes and compresses vocal
        #      dynamics into audible distortion.  The 2 s window still provides
        #      correct station-to-station loudness normalisation while being
        #      transparent within a programme.
        if rms > _AGC_GATE:
            self._rms_k_smooth += 0.05 * (rms_k - self._rms_k_smooth)
            target_gain = 0.12 / (self._rms_k_smooth + 1e-10)
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
