"""
HD Radio (NRSC-5 IBOC) sideband detection on analog FM — single tuner.

Hybrid IBOC places OFDM digital sidebands at roughly ±129–198 kHz from the
analog carrier.  The FM pipeline captures ±600 kHz of raw IQ but decimates
to ±120 kHz for audio, so the sideband region is captured and then thrown
away — this detector looks at it before that happens.

Method: averaged periodogram over one IQ block.  The sidebands appear as
flat, noise-like energy blocks *symmetric about the carrier*; mean power in
both sideband regions is compared against a reference band (±230–290 kHz,
normally just noise floor).  Requiring both sidebands elevated, roughly
symmetric, and persistent (EMA + hysteresis) rejects the common false
positives: adjacent-channel splatter (one-sided) and momentary noise.

Thresholds are conservative first guesses — `hd_ratio` is exposed in the
diagnostics feed so they can be calibrated against real stations.
"""

import numpy as np

_FS   = 1_200_000   # pipeline FM sample rate
_NFFT = 8_192
_SEGS = 8           # segments averaged per process() call


class HdSidebandDetector:
    SB_LO,  SB_HI  = 135_000, 195_000   # IBOC primary sidebands (±129.4–198.4 kHz)
    REF_LO, REF_HI = 230_000, 290_000   # reference: noise floor beyond the sidebands
    ON_RATIO  = 2.5    # smoothed sideband/reference power ratio to declare HD
    OFF_RATIO = 1.8    # hysteresis release
    SYM_MAX   = 4.0    # max upper/lower sideband imbalance (linear power)
    EMA_ALPHA = 0.4

    def __init__(self):
        freqs = np.fft.fftshift(np.fft.fftfreq(_NFFT, 1.0 / _FS))
        self._sb_pos = (freqs >=  self.SB_LO) & (freqs <=  self.SB_HI)
        self._sb_neg = (freqs <= -self.SB_LO) & (freqs >= -self.SB_HI)
        af = np.abs(freqs)
        self._ref    = (af >= self.REF_LO) & (af <= self.REF_HI)
        self._window = np.hanning(_NFFT).astype(np.float32)
        self.ratio: float = 0.0       # smoothed detection metric (diagnostics)
        self.available: bool = False

    def process(self, iq: np.ndarray):
        """Feed one raw IQ block (any length ≥ _NFFT × _SEGS samples)."""
        n = _NFFT * _SEGS
        if len(iq) < n:
            return
        x = np.asarray(iq[:n]).reshape(_SEGS, _NFFT) * self._window
        psd = np.fft.fftshift(
            np.mean(np.abs(np.fft.fft(x, axis=1)) ** 2, axis=0)
        )

        p_pos = float(psd[self._sb_pos].mean())
        p_neg = float(psd[self._sb_neg].mean())
        p_ref = float(psd[self._ref].mean()) + 1e-20

        lo, hi = min(p_pos, p_neg), max(p_pos, p_neg)
        symmetric = hi / (lo + 1e-20) <= self.SYM_MAX
        # One-sided energy is adjacent-channel splatter, not IBOC — score 0
        raw = (lo / p_ref) if symmetric else 0.0

        self.ratio += self.EMA_ALPHA * (raw - self.ratio)
        if self.available:
            if self.ratio < self.OFF_RATIO:
                self.available = False
        elif self.ratio > self.ON_RATIO:
            self.available = True
