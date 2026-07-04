"""
Stateful streaming DSP helpers shared by the FM, AM, and RDS paths.

Both classes exist to fix the same defect: per-block calls to stateless
scipy functions (`resample_poly`, FFT `hilbert`) restart their filters at
every block boundary and inject a periodic edge transient at the block
rate (~4.6 Hz).  For audio that was a subtle artifact; for RDS it
corrupted bits near every boundary.

StatefulResampler — overlap-save wrapper around `resample_poly` that
    maintains input history across calls and emits only outputs whose
    full filter context was available.  The emitted samples form one
    globally uniform output grid, bit-identical (interior) to running
    `resample_poly` over the whole concatenated signal at once.

PilotRecovery — analytic 19 kHz pilot extraction for carrier synthesis.
    Replaces the old BPF → FFT-hilbert construction, which was stateless
    and left phase discontinuities at block edges in the 38/57 kHz
    carriers.  Heterodyne to DC → stateful complex lowpass → re-rotate is
    edge-free, cheaper (no large FFTs), and preserves the pilot carrier
    phase exactly like the old symmetric BPF did (both have zero phase
    shift at the pilot frequency); the lowpass group delay lands on the
    pilot *envelope*, which is constant for an unmodulated pilot, so
    steady-state carrier phase is unaffected.
"""

import math

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt


class StatefulResampler:
    """Streaming rational resampler (up/down) with continuous filter state.

    Output sample k (global index) corresponds to input time k*down/up in
    input-sample units, exactly matching `resample_poly`'s zero-phase grid
    anchored at the very first input sample ever fed.
    """

    def __init__(self, up: int, down: int):
        self._up = up
        self._down = down
        g = math.gcd(up, down)
        # Local resample_poly grids anchor at ext[0]; the history cut must
        # land on a global grid point, i.e. base % (down/gcd) == 0.
        self._q = down // g
        # Filter half-length in *input* samples: resample_poly's default
        # window has half_len = 10*max(up, down) taps at the up-rate.
        self._h_in = -(-10 * max(up, down) // up) + 1   # ceil, +1 margin
        self._hist = None          # trailing input samples starting at _base
        self._base = 0             # absolute input index of _hist[0]
        self._total = 0            # absolute input samples consumed
        self._next_k = 0           # next global output index to emit

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x)
        if self._hist is None or len(self._hist) == 0:
            ext = x
        else:
            ext = np.concatenate((self._hist, x))
        self._total += len(x)

        y = resample_poly(ext, self._up, self._down)
        # y[m] sits at absolute input time base + m*down/up; base is on the
        # global grid, so global index k = m + base*up/down (exact integer).
        k_off = self._base * self._up // self._down

        # Emit every output whose right filter context is fully available.
        k_hi = (self._total - 1 - self._h_in) * self._up // self._down
        if k_hi >= self._next_k:
            out = y[self._next_k - k_off: k_hi - k_off + 1]
            self._next_k = k_hi + 1
        else:
            out = y[:0]

        # Keep enough history for the next call's left context, cut on a
        # global grid point.
        b = max(self._base, self._q * ((self._total - 2 * self._h_in - 1) // self._q))
        self._hist = ext[b - self._base:]
        self._base = b
        return out


class PilotRecovery:
    """Extract the analytic pilot A·e^{jθ(t)} from the FM composite.

    Fully stateful: complex mixer phase is continuous across blocks and
    the lowpass carries sosfilt zi, so consecutive process() calls behave
    exactly like one long call.  The nominal mixer frequency only needs
    to be within the lowpass bandwidth of the true pilot; the actual
    received phase/frequency offset survives in the complex envelope.
    """

    def __init__(self, fs: int, f0: float = 19_000.0, bw: float = 2_000.0,
                 order: int = 4):
        self._sos = butter(order, bw, 'lowpass', fs=fs, output='sos')
        self._zi = np.zeros((self._sos.shape[0], 2), dtype=np.complex128)
        self._w = 2.0 * np.pi * f0 / fs
        self._phase0 = 0.0     # mixer phase at the next sample, kept mod 2π

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        ph = self._phase0 + self._w * np.arange(n)
        self._phase0 = (self._phase0 + self._w * n) % (2.0 * np.pi)
        rot = np.exp(-1j * ph)
        base = x * rot
        base, self._zi = sosfilt(self._sos, base, zi=self._zi)
        # ×2 restores the analytic-signal convention: A·cos θ mixed and
        # lowpassed leaves (A/2)·e^{jΔ}; doubling recovers A·e^{jθ}.
        return 2.0 * base * np.conj(rot)
