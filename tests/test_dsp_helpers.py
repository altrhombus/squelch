"""Streaming-equivalence tests for the stateful DSP helpers.

StatefulResampler must be bit-identical to a one-shot resample_poly over
the concatenated signal, regardless of how the input is split into
blocks — that equivalence is the whole point of the class (the old
per-block resample_poly restarted its filter at every block edge).
"""

import numpy as np
import pytest
from scipy.signal import resample_poly

from backend.sdr.dsp import PilotRecovery, StatefulResampler

# Awkward split sizes on purpose: primes, tiny blocks, pipeline sizes.
SPLITS = [131072, 52429, 7, 262144, 99991]


@pytest.mark.parametrize("up,down", [
    (1, 5),      # FM IQ 1.2 MHz → 240 kHz
    (19, 240),   # RDS baseband 240 kHz → 19 kHz
    (1, 25),     # AM/NFM IQ 1.2 MHz → 48 kHz
])
def test_streamed_output_matches_one_shot(up, down):
    rng = np.random.default_rng(0)
    x = rng.standard_normal(600_000)
    ref = resample_poly(x, up, down)

    sr = StatefulResampler(up, down)
    outs, i = [], 0
    for size in SPLITS:
        outs.append(sr.process(x[i:i + size]))
        i += size
    outs.append(sr.process(x[i:]))
    y = np.concatenate(outs)

    # The streamed version holds back the last few outputs (no right
    # context yet), but everything it emits must match exactly.
    assert len(y) >= len(ref) - 4 * down
    np.testing.assert_array_equal(y, ref[:len(y)])


def test_complex_input_preserved():
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(300_000)
         + 1j * rng.standard_normal(300_000)).astype(np.complex64)
    ref = resample_poly(x, 1, 5)
    sr = StatefulResampler(1, 5)
    y = np.concatenate([sr.process(x[:262_144]), sr.process(x[262_144:])])
    assert np.iscomplexobj(y)
    np.testing.assert_array_equal(y, ref[:len(y)])


def test_pilot_recovery_is_block_continuous():
    """Recovered analytic pilot must have no phase error spikes at block
    boundaries, and tolerate a realistic pilot frequency offset."""
    fs = 240_000
    n = 300_000
    t = np.arange(n) / fs
    theta = 2 * np.pi * 19_000.57 * t + 0.7   # ~30 ppm offset + phase
    comp = (0.4 * np.sin(2 * np.pi * 900 * t)
            + 0.1 * np.cos(theta)
            + 0.3 * np.cos(2 * theta) * np.sin(2 * np.pi * 2_500 * t))

    pr = PilotRecovery(fs)
    edges = [52_429, 104_858, 157_287]
    blocks = np.split(comp, edges)
    a = np.concatenate([pr.process(b) for b in blocks])

    ideal = 0.1 * np.exp(1j * theta)
    seg = slice(5_000, n)   # skip filter warmup
    ph_err = np.abs(np.angle(a[seg] * np.conj(ideal[seg])))
    assert ph_err.max() < np.deg2rad(3.0)

    # Boundary neighbourhoods specifically: no elevated error.
    for b in edges:
        r = slice(b - 30, b + 30)
        e = np.abs(np.angle(a[r] * np.conj(ideal[r])))
        assert e.max() < np.deg2rad(3.0)

    # Amplitude convention: |analytic| = pilot amplitude, so the RMS
    # measure used for the blend gates is A/√2.
    rms = float(np.sqrt(np.mean(np.abs(a[seg]) ** 2) / 2))
    assert abs(rms - 0.1 / np.sqrt(2)) < 0.005
