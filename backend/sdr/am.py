"""
AM and NFM (scanner / weather) demodulators.

Both take complex64 IQ at 1.2 MHz and return float32 mono at 48 kHz.

AM:  channel filter → envelope detector → audio LPF → DC removal → AGC.
     The channel filter matters: decimation alone leaves a ±24 kHz
     passband, which on the 10 kHz-spaced US AM band admits two adjacent
     stations per side — their carriers beat against the envelope
     detector as 10/20 kHz heterodyne whistles.  A ±5.5 kHz complex
     lowpass isolates the tuned channel before |·|.
NFM: channel filter (±10 kHz, Carson bandwidth for 5 kHz deviation voice
     plus tuning-offset tolerance) → phase discriminator → audio LPF →
     spectral noise reduction → AGC.  Used for scanner bands and NOAA
     weather radio (162.4–162.55 MHz).

Both demods carry a carrier-offset estimator over the full ±24 kHz
decimated passband; the pipeline's AFC uses it to recentre the tuner on
narrowband channels, where a generic dongle's crystal error (~100 ppm ≈
16 kHz at 162 MHz) otherwise parks the signal outside the channel filter.

Decimation is stateful (StatefulResampler) and the NFM discriminator
carries its last sample across blocks, so block boundaries are seamless.
"""

import numpy as np
from scipy.signal import butter, sosfilt

from .dsp import StatefulResampler
# The FM path's MinStat + decision-directed Wiener noise reducer — shared
# here for NFM/WX voice (MinStat needs no silence detection, so NOAA's
# continuous broadcast is fine).
from .fm import _SpectralSubtractor

_SAMPLE_RATE = 1_200_000
_AUDIO_RATE  = 48_000
_DECIM       = _SAMPLE_RATE // _AUDIO_RATE   # 25
_NFM_DEV     = 5_000                          # NFM deviation Hz


def _zero_zi(sos: np.ndarray) -> np.ndarray:
    return np.zeros((sos.shape[0], 2), dtype=np.float64)


def _zero_zi_c(sos: np.ndarray) -> np.ndarray:
    return np.zeros((sos.shape[0], 2), dtype=np.complex128)


def _agc_step(gain: float, rms: float) -> float:
    """Shared asymmetric AGC: fast attack / slow release toward 0.25 RMS.

    Gated on silence: during speech pauses the target (0.25/near-zero)
    is enormous and even the slow release adds gain at ~2.5×/block, so
    the first word after a pause came back +9-12 dB hot and clipping
    (measured on a WX recording — heard as harsh sibilants).  Freezing
    the gain while the post-gain level is below the gate holds the last
    speech-appropriate value; the FM path gates its AGC the same way.
    """
    if rms * gain <= 0.04:
        return gain
    target = 0.25 / rms
    if target < gain:
        gain = 0.5 * gain + 0.5 * target     # fast attack
    else:
        gain = 0.97 * gain + 0.03 * target   # slow release
    return float(np.clip(gain, 0.05, 50.0))


class _CarrierOffsetEstimator:
    """Strongest signal's offset from the tuned frequency via power
    centroid over the decimated passband.

    The centroid, not the peak: a modulated spectrum spreads across its
    bandwidth and an FM carrier line can vanish outright (Bessel null),
    but the spectrum stays symmetric about the carrier, so the power
    centroid of the significant bins reads the true offset regardless of
    modulation.  `updates` is monotonic so the AFC can tell fresh
    measurements from stale ones (e.g. while squelched)."""

    def __init__(self, fs: int):
        self._fs = fs
        self._init = False
        self.offset_hz: float = 0.0
        self.updates: int = 0

    def feed(self, x: np.ndarray):
        p = np.abs(np.fft.fft(x * np.hanning(len(x)))) ** 2
        mask = p > 0.1 * p.max()
        freqs = np.fft.fftfreq(len(x), 1.0 / self._fs)
        off = float(np.sum(freqs[mask] * p[mask]) / np.sum(p[mask]))
        if self._init:
            self.offset_hz += 0.3 * (off - self.offset_hz)
        else:
            self.offset_hz = off
            self._init = True
        self.updates += 1

    def reset(self):
        """Restart the EMA (after an AFC hop moved the carrier)."""
        self._init = False


class AmDemodulator:
    """
    AM envelope demodulator: channel-filtered, with DC removal and soft AGC.
    """

    SAMPLE_RATE = _SAMPLE_RATE
    AUDIO_RATE  = _AUDIO_RATE

    last_iq_rms: float = 0.0   # read by pipeline.py for squelch

    def __init__(self):
        self._decim = StatefulResampler(1, _DECIM)
        self.carrier_offset = _CarrierOffsetEstimator(_AUDIO_RATE)
        # Channel filter: complex lowpass ±5.5 kHz on the decimated IQ.
        # Keeps the tuned carrier + sidebands; rejects the adjacent
        # channels (±10/20 kHz) that otherwise heterodyne into whistles.
        self._chan_sos = butter(6, 5_500, 'lowpass', fs=_AUDIO_RATE, output='sos')
        self._chan_zi  = _zero_zi_c(self._chan_sos)
        # Audio lowpass after envelope detection: the detector is
        # nonlinear, so it regenerates HF intermod products above the
        # channel bandwidth; AM audio content tops out around 5 kHz.
        self._aud_sos = butter(4, 5_000, 'lowpass', fs=_AUDIO_RATE, output='sos')
        self._aud_zi  = _zero_zi(self._aud_sos)
        # DC blocker: highpass at 30 Hz (removes the carrier offset)
        self._dc_sos = butter(2, 30, 'highpass', fs=_AUDIO_RATE, output='sos')
        self._dc_zi  = _zero_zi(self._dc_sos)
        self._gain   = 1.0

    @property
    def last_carrier_offset_hz(self) -> float:
        return self.carrier_offset.offset_hz

    def process(self, iq: np.ndarray) -> np.ndarray:
        self.last_iq_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))

        decimated = self._decim.process(iq)
        if decimated.size == 0:
            return np.zeros(0, dtype=np.float32)

        self.carrier_offset.feed(decimated)

        # Channel isolation, then AM envelope
        chan, self._chan_zi = sosfilt(self._chan_sos, decimated, zi=self._chan_zi)
        env = np.abs(chan).astype(np.float64)

        # DC removal, then audio bandwidth limit
        env, self._dc_zi  = sosfilt(self._dc_sos,  env, zi=self._dc_zi)
        env, self._aud_zi = sosfilt(self._aud_sos, env, zi=self._aud_zi)

        rms = float(np.sqrt(np.mean(env ** 2))) + 1e-10
        self._gain = _agc_step(self._gain, rms)
        env = (env * self._gain).clip(-1.0, 1.0)

        return env.astype(np.float32)


class NfmDemodulator:
    """
    Narrowband FM demodulator (5 kHz deviation: PMR, repeaters, NOAA WX).
    """

    SAMPLE_RATE = _SAMPLE_RATE
    AUDIO_RATE  = _AUDIO_RATE

    last_iq_rms: float = 0.0   # read by pipeline.py for squelch

    def __init__(self, noise_reduction: bool = True):
        self._decim = StatefulResampler(1, _DECIM)
        self.carrier_offset = _CarrierOffsetEstimator(_AUDIO_RATE)
        # Channel filter before the discriminator: Carson bandwidth for
        # 5 kHz deviation + ~3 kHz voice ≈ ±8 kHz, opened to ±10 kHz for
        # tolerance to residual tuning offset (NOAA WX channels are 25 kHz
        # apart, so selectivity is unaffected).  Without it the
        # discriminator sees the full ±24 kHz decimated passband and
        # adjacent-channel energy lands directly in the audio.
        self._chan_sos = butter(6, 10_000, 'lowpass', fs=_AUDIO_RATE, output='sos')
        self._chan_zi  = _zero_zi_c(self._chan_sos)
        # Audio lowpass 4 kHz to suppress inter-channel noise
        self._lp_sos = butter(4, 4_000, 'lowpass', fs=_AUDIO_RATE, output='sos')
        self._lp_zi  = _zero_zi(self._lp_sos)
        # Discriminator noise floor, measured above the voice band
        # (6-20 kHz — no programme content there) exactly like the FM
        # path's 65-90 kHz measurement.  This drives the spectral
        # subtractor's physics floor; MinStat alone stores raw per-frame
        # powers, whose 128-frame minimum sits near mean/128 for
        # exponential bin statistics — far too low to subtract against
        # (the FM path has always leaned on its physics floor for the
        # same reason).  Empirical scale ≈ 211 vs the subtractor's
        # default 200: the default slightly under-estimates, the safe
        # direction.
        self._noise_sos = butter(4, [6_000, 20_000], 'bandpass', fs=_AUDIO_RATE, output='sos')
        self._noise_zi  = _zero_zi(self._noise_sos)
        self._noise_rms_smooth = 0.0
        self._noise_rms_init   = False
        self._prev   = None    # last channel sample — discriminator carry
        # Spectral noise reduction between audio LPF and AGC.  The
        # conservative weak-signal floor (signal_quality=0, −12 dB) is a
        # sensible fixed operating point for narrowband voice; MinStat
        # provides the noise estimate with no silence gating needed —
        # speech pauses (inter-word gaps) are what it keys on.  Known
        # caveat: a stationary tone longer than MinStat's ~1.4 s window
        # (e.g. a NOAA alert tone) gets classified as noise floor and
        # rides at the −12 dB floor — the AGC downstream re-normalises
        # the level, so it stays clearly audible, just with less margin.
        self._ss = _SpectralSubtractor() if noise_reduction else None
        self._gain   = 1.0

    @property
    def last_carrier_offset_hz(self) -> float:
        return self.carrier_offset.offset_hz

    def process(self, iq: np.ndarray) -> np.ndarray:
        self.last_iq_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))

        decimated = self._decim.process(iq)
        if decimated.size == 0:
            return np.zeros(0, dtype=np.float32)

        self.carrier_offset.feed(decimated)

        chan, self._chan_zi = sosfilt(self._chan_sos, decimated, zi=self._chan_zi)

        # FM discriminator — continuous and length-preserving across blocks
        prev = self._prev if self._prev is not None else chan[:1]
        self._prev = chan[-1:].copy()
        z     = chan * np.conj(np.concatenate((prev, chan[:-1])))
        audio = (np.angle(z) * (_AUDIO_RATE / (2.0 * np.pi * _NFM_DEV))).astype(np.float64)

        # Noise floor measurement on the raw discriminator output (the
        # audio LPF below would remove the 6-20 kHz measurement band).
        nb, self._noise_zi = sosfilt(self._noise_sos, audio, zi=self._noise_zi)
        nrms = float(np.sqrt(np.mean(nb ** 2)))
        if not self._noise_rms_init:
            self._noise_rms_smooth = nrms
            self._noise_rms_init   = True
        else:
            self._noise_rms_smooth += 0.05 * (nrms - self._noise_rms_smooth)

        audio, self._lp_zi = sosfilt(self._lp_sos, audio, zi=self._lp_zi)

        if self._ss is not None:
            audio = self._ss.process(audio.astype(np.float32),
                                     update_noise=False,
                                     noise_rms_smooth=self._noise_rms_smooth,
                                     ).astype(np.float64)

        rms = float(np.sqrt(np.mean(audio ** 2))) + 1e-10
        self._gain = _agc_step(self._gain, rms)
        audio = (audio * self._gain).clip(-1.0, 1.0)

        return audio.astype(np.float32)
