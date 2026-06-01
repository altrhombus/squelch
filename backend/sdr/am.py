"""
AM and NFM (scanner) demodulators.

Both take complex64 IQ at 1.2 MHz and return float32 mono at 48 kHz.

AM:  envelope detector — |IQ| with DC removal and AGC normalisation
NFM: narrowband FM discriminator — phase difference, 5 kHz deviation
     Aviation band (118-137 MHz) uses AM automatically (see pipeline.py)
"""

import numpy as np
from scipy.signal import butter, firwin, sosfilt, sosfilt_zi, lfilter, resample_poly

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

    def __init__(self):
        # DC blocker: highpass at 30 Hz (removes envelope offset)
        self._dc_sos = butter(2, 30, 'highpass', fs=_AUDIO_RATE, output='sos')
        self._dc_zi  = _zero_zi(self._dc_sos)
        self._gain   = 1.0
        # Stateful FIR decimation (same reasoning as fm.py)
        _h = firwin(65, 1.0 / _DECIM).astype(np.float64)
        self._fir_h    = _h
        self._fir_zi_r = np.zeros(64)
        self._fir_zi_i = np.zeros(64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        iq64 = iq.astype(np.complex128)
        fr, self._fir_zi_r = lfilter(self._fir_h, [1.0], iq64.real, zi=self._fir_zi_r)
        fi, self._fir_zi_i = lfilter(self._fir_h, [1.0], iq64.imag, zi=self._fir_zi_i)
        decimated = (fr[::_DECIM] + 1j * fi[::_DECIM]).astype(np.complex64)

        # AM envelope
        env = np.abs(decimated).astype(np.float64)

        # DC removal
        env, self._dc_zi = sosfilt(self._dc_sos, env, zi=self._dc_zi)

        # Soft AGC: normalise so RMS ≈ 0.25
        rms = float(np.sqrt(np.mean(env ** 2))) + 1e-10
        target = 0.25
        self._gain = 0.95 * self._gain + 0.05 * (target / rms)
        env = (env * self._gain).clip(-1.0, 1.0)

        return env.astype(np.float32)


class NfmDemodulator:
    """
    Narrowband FM demodulator (5 kHz deviation, e.g. PMR, repeaters).
    """

    SAMPLE_RATE = _SAMPLE_RATE
    AUDIO_RATE  = _AUDIO_RATE

    def __init__(self):
        # Audio lowpass 4 kHz to suppress inter-channel noise
        self._lp_sos = butter(4, 4_000, 'lowpass', fs=_AUDIO_RATE, output='sos')
        self._lp_zi  = _zero_zi(self._lp_sos)
        self._gain   = 1.0
        _h = firwin(65, 1.0 / _DECIM).astype(np.float64)
        self._fir_h    = _h
        self._fir_zi_r = np.zeros(64)
        self._fir_zi_i = np.zeros(64)

    def process(self, iq: np.ndarray) -> np.ndarray:
        iq64 = iq.astype(np.complex128)
        fr, self._fir_zi_r = lfilter(self._fir_h, [1.0], iq64.real, zi=self._fir_zi_r)
        fi, self._fir_zi_i = lfilter(self._fir_h, [1.0], iq64.imag, zi=self._fir_zi_i)
        decimated = (fr[::_DECIM] + 1j * fi[::_DECIM]).astype(np.complex64)

        # FM discriminator
        z     = decimated[1:] * np.conj(decimated[:-1])
        audio = (np.angle(z) * (_AUDIO_RATE / (2.0 * np.pi * _NFM_DEV))).astype(np.float64)

        # Lowpass + AGC
        audio, self._lp_zi = sosfilt(self._lp_sos, audio, zi=self._lp_zi)

        rms = float(np.sqrt(np.mean(audio ** 2))) + 1e-10
        self._gain = 0.95 * self._gain + 0.05 * (0.25 / rms)
        audio = (audio * self._gain).clip(-1.0, 1.0)

        return audio.astype(np.float32)
