"""
AM and NFM (scanner) demodulators.

Both take complex64 IQ at 1.2 MHz and return float32 mono at 48 kHz.

AM:  envelope detector — |IQ| with DC removal and AGC normalisation
NFM: narrowband FM discriminator — phase difference, 5 kHz deviation
     Aviation band (118-137 MHz) uses AM automatically (see pipeline.py)
"""

import numpy as np
from scipy.signal import butter, sosfilt, resample_poly

_SAMPLE_RATE = 1_200_000
_AUDIO_RATE  = 48_000
_DECIM       = _SAMPLE_RATE // _AUDIO_RATE   # 25
_NFM_DEV     = 5_000                          # NFM deviation Hz


def _zero_zi(sos: np.ndarray) -> np.ndarray:
    return np.zeros((sos.shape[0], 2), dtype=np.float64)


class AmDemodulator:
    """
    AM envelope demodulator with DC removal and soft AGC.
    """

    SAMPLE_RATE = _SAMPLE_RATE
    AUDIO_RATE  = _AUDIO_RATE

    last_iq_rms: float = 0.0   # read by pipeline.py for squelch

    def __init__(self):
        # DC blocker: highpass at 30 Hz (removes envelope offset)
        self._dc_sos = butter(2, 30, 'highpass', fs=_AUDIO_RATE, output='sos')
        self._dc_zi  = _zero_zi(self._dc_sos)
        self._gain   = 1.0

    def process(self, iq: np.ndarray) -> np.ndarray:
        self.last_iq_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))

        decimated = resample_poly(iq, 1, _DECIM)

        # AM envelope
        env = np.abs(decimated).astype(np.float64)

        # DC removal
        env, self._dc_zi = sosfilt(self._dc_sos, env, zi=self._dc_zi)

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
    Narrowband FM demodulator (5 kHz deviation, e.g. PMR, repeaters).
    """

    SAMPLE_RATE = _SAMPLE_RATE
    AUDIO_RATE  = _AUDIO_RATE

    last_iq_rms: float = 0.0   # read by pipeline.py for squelch

    def __init__(self):
        # Audio lowpass 4 kHz to suppress inter-channel noise
        self._lp_sos = butter(4, 4_000, 'lowpass', fs=_AUDIO_RATE, output='sos')
        self._lp_zi  = _zero_zi(self._lp_sos)
        self._gain   = 1.0

    def process(self, iq: np.ndarray) -> np.ndarray:
        self.last_iq_rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))

        decimated = resample_poly(iq, 1, _DECIM)

        # FM discriminator
        z     = decimated[1:] * np.conj(decimated[:-1])
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
