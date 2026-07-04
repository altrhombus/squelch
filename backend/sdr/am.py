"""
AM and NFM (scanner / weather) demodulators.

Both take complex64 IQ at 1.2 MHz and return float32 mono at 48 kHz.

AM:  channel filter → envelope detector → audio LPF → DC removal → AGC.
     The channel filter matters: decimation alone leaves a ±24 kHz
     passband, which on the 10 kHz-spaced US AM band admits two adjacent
     stations per side — their carriers beat against the envelope
     detector as 10/20 kHz heterodyne whistles.  A ±5.5 kHz complex
     lowpass isolates the tuned channel before |·|.
NFM: channel filter (±8 kHz, Carson bandwidth for 5 kHz deviation voice)
     → phase discriminator → audio LPF → AGC.  Used for scanner bands
     and NOAA weather radio (162.4–162.55 MHz).

Decimation is stateful (StatefulResampler) and the NFM discriminator
carries its last sample across blocks, so block boundaries are seamless.
"""

import numpy as np
from scipy.signal import butter, sosfilt

from .dsp import StatefulResampler

_SAMPLE_RATE = 1_200_000
_AUDIO_RATE  = 48_000
_DECIM       = _SAMPLE_RATE // _AUDIO_RATE   # 25
_NFM_DEV     = 5_000                          # NFM deviation Hz


def _zero_zi(sos: np.ndarray) -> np.ndarray:
    return np.zeros((sos.shape[0], 2), dtype=np.float64)


def _zero_zi_c(sos: np.ndarray) -> np.ndarray:
    return np.zeros((sos.shape[0], 2), dtype=np.complex128)


class AmDemodulator:
    """
    AM envelope demodulator: channel-filtered, with DC removal and soft AGC.
    """

    SAMPLE_RATE = _SAMPLE_RATE
    AUDIO_RATE  = _AUDIO_RATE

    last_iq_rms: float = 0.0   # read by pipeline.py for squelch

    def __init__(self):
        self._decim = StatefulResampler(1, _DECIM)
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

    def process(self, iq: np.ndarray) -> np.ndarray:
        self.last_iq_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))

        decimated = self._decim.process(iq)
        if decimated.size == 0:
            return np.zeros(0, dtype=np.float32)

        # Channel isolation, then AM envelope
        chan, self._chan_zi = sosfilt(self._chan_sos, decimated, zi=self._chan_zi)
        env = np.abs(chan).astype(np.float64)

        # DC removal, then audio bandwidth limit
        env, self._dc_zi  = sosfilt(self._dc_sos,  env, zi=self._dc_zi)
        env, self._aud_zi = sosfilt(self._aud_sos, env, zi=self._aud_zi)

        # Asymmetric AGC: fast attack (loud signal) / slow release (quiet).
        # Prevents clipping on sudden loud carriers while avoiding pumping
        # artefacts on voice.
        rms = float(np.sqrt(np.mean(env ** 2))) + 1e-10
        target_gain = 0.25 / rms
        if target_gain < self._gain:
            self._gain = 0.5 * self._gain + 0.5 * target_gain   # fast attack
        else:
            self._gain = 0.97 * self._gain + 0.03 * target_gain  # slow release
        env = (env * self._gain).clip(-1.0, 1.0)

        return env.astype(np.float32)


class NfmDemodulator:
    """
    Narrowband FM demodulator (5 kHz deviation: PMR, repeaters, NOAA WX).
    """

    SAMPLE_RATE = _SAMPLE_RATE
    AUDIO_RATE  = _AUDIO_RATE

    last_iq_rms: float = 0.0   # read by pipeline.py for squelch

    def __init__(self):
        self._decim = StatefulResampler(1, _DECIM)
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
        self._prev   = None    # last channel sample — discriminator carry
        self._gain   = 1.0

    def process(self, iq: np.ndarray) -> np.ndarray:
        self.last_iq_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))

        decimated = self._decim.process(iq)
        if decimated.size == 0:
            return np.zeros(0, dtype=np.float32)

        chan, self._chan_zi = sosfilt(self._chan_sos, decimated, zi=self._chan_zi)

        # FM discriminator — continuous and length-preserving across blocks
        prev = self._prev if self._prev is not None else chan[:1]
        self._prev = chan[-1:].copy()
        z     = chan * np.conj(np.concatenate((prev, chan[:-1])))
        audio = (np.angle(z) * (_AUDIO_RATE / (2.0 * np.pi * _NFM_DEV))).astype(np.float64)

        # Lowpass + asymmetric AGC
        audio, self._lp_zi = sosfilt(self._lp_sos, audio, zi=self._lp_zi)

        rms = float(np.sqrt(np.mean(audio ** 2))) + 1e-10
        target_gain = 0.25 / rms
        if target_gain < self._gain:
            self._gain = 0.5 * self._gain + 0.5 * target_gain   # fast attack
        else:
            self._gain = 0.97 * self._gain + 0.03 * target_gain  # slow release
        audio = (audio * self._gain).clip(-1.0, 1.0)

        return audio.astype(np.float32)
