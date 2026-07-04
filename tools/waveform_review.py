#!/usr/bin/env python3
"""Offline waveform review for Squelch recordings.

Drop any recording (AAC or anything PyAV decodes) through a battery of
objective checks.  Ears decide what the audio *should* sound like;
this tool defends that verdict — each check exists because a real
artifact was once heard, recorded, and root-caused:

  levels     RMS vs the AGC target (0.12 K-ish for FM, 0.25 for NFM/AM),
             peak headroom, limiter engagement, DC offset
  spectrum   band balance and the encoder's HF rolloff (missing
             de-emphasis or a wrong encoder cutoff shows up here)
  stereo     side/mid energy — how far the blend gate is narrowing
  warble     HF (3.5-8 kHz) envelope modulation at the spectral
             subtractor's hop rate (~94 Hz).  The "harsh sibilants"
             artifact of an over-deep Wiener floor reads > +3 dB here;
             healthy audio reads ~0 dB
  onsets     level of the first ~160 ms of speech after each pause vs
             the settled level that follows.  The ungated NFM AGC blasted
             onsets at +9-12 dB (near clipping); healthy is < +3 dB

Usage:
    python tools/waveform_review.py RECORDING [RECORDING ...]
"""

import sys

import av
import numpy as np
from scipy.signal import butter, sosfilt


def load(path):
    """Decode to (channels × samples) float64 plus sample rate."""
    container = av.open(path)
    stream = container.streams.audio[0]
    chunks = []
    for frame in container.decode(stream):
        a = frame.to_ndarray()
        if a.ndim == 1:
            a = a[None, :]
        if a.shape[0] == 1 and stream.codec_context.channels == 2:
            a = a.reshape(-1, 2).T          # packed non-planar stereo
        chunks.append(a)
    return np.concatenate(chunks, axis=1).astype(np.float64), \
        stream.codec_context.sample_rate


def band_power(x, sr, lo, hi):
    n = min(len(x), 2 ** 19)
    s = np.abs(np.fft.rfft(x[:n] * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, 1 / sr)
    m = (f >= lo) & (f < hi)
    return float(s[m].mean()) if m.any() else 0.0


def hop_warble_db(x, sr):
    """Excess HF-envelope modulation at the subtractor hop rate (~94 Hz)
    over neighbouring modulation frequencies.  > ~+3 dB = Wiener gain
    warble (the "harsh S" artifact); healthy audio reads about 0."""
    sos = butter(4, [3_500, 8_000], 'bandpass', fs=sr, output='sos')
    hf = sosfilt(sos, x)
    hop = sr // 1000
    env = np.sqrt(np.convolve(hf ** 2, np.ones(hop) / hop, 'same'))[::hop]
    env = env - env.mean()
    n = min(len(env), 2 ** 16)
    S = np.abs(np.fft.rfft(env[:n] * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, 1 / 1000.0)
    at_hop = S[(f > 88) & (f < 100)].mean()
    nearby = S[((f > 60) & (f < 85)) | ((f > 103) & (f < 130))].mean()
    return 10 * np.log10(at_hop / (nearby + 1e-30))


def onset_overshoots(x, sr):
    """(time, overshoot_dB) for each speech onset following a >=0.3 s
    pause: peak envelope of the first ~160 ms vs the settled level
    0.3-0.8 s later."""
    hop = sr // 50                     # 20 ms envelope grid
    env = np.sqrt(np.convolve(x ** 2, np.ones(hop) / hop, 'same'))[::hop]
    t = np.arange(len(env)) / 50.0
    speech = np.percentile(env, 75)
    quiet = env < 0.25 * speech
    out = []
    i = 15
    while i < len(env) - 50:
        if quiet[i] and not quiet[i + 1] and quiet[i - 15:i].all():
            first = env[i + 1:i + 9].max()
            steady = np.median(env[i + 15:i + 40])
            if steady > 0.05 * speech:
                out.append((t[i], 20 * np.log10(first / (steady + 1e-12))))
            i += 10
        i += 1
    return out


def review(path):
    x, sr = load(path)
    L = x[0]
    R = x[1] if x.shape[0] == 2 else x[0]
    mono = 0.5 * (L + R)
    side = 0.5 * (L - R)

    print(f"\n═══ {path}")
    print(f"    {len(mono)/sr:.0f} s, {x.shape[0]} ch, {sr} Hz")

    for name, v in (("L", L), ("R", R)):
        print(f"  {name}: rms={np.sqrt(np.mean(v**2)):.3f}"
              f"  peak={np.abs(v).max():.3f}"
              f"  DC={v.mean():+.5f}"
              f"  |x|>0.95: {(np.abs(v) > 0.95).mean()*100:.3f}%")

    if x.shape[0] == 2:
        sm = 10 * np.log10(np.mean(side ** 2) / (np.mean(mono ** 2) + 1e-30))
        print(f"  stereo side/mid: {sm:+.1f} dB"
              "   (full stereo ≈ −6..−12; heavy blend < −20)")

    bands = [(50, 200), (200, 1000), (1000, 4000), (4000, 8000),
             (8000, 12000), (12000, 15000), (15500, min(20000, sr // 2))]
    ref = band_power(mono, sr, 1000, 4000) + 1e-30

    def label(lo, hi):
        return f"{lo // 1000 if lo >= 1000 else lo}-{hi // 1000}k"

    print("  spectrum rel 1-4k: "
          + "  ".join(f"{label(lo, hi)}:{10*np.log10(band_power(mono, sr, lo, hi)/ref):+.0f}dB"
                      for lo, hi in bands))

    w = hop_warble_db(mono, sr)
    print(f"  hop-rate warble: {w:+.1f} dB"
          + ("   ⚠ Wiener gain artifact" if w > 3.0 else "   (ok)"))

    onsets = onset_overshoots(mono, sr)
    if onsets:
        dbs = [d for _, d in onsets]
        # Flag on the MEDIAN: a misbehaving AGC blasts every onset
        # uniformly (pre-fix WX read +8.9 dB median), while natural
        # speech prosody produces scattered outliers around a low median
        # (a host genuinely emphasises the first word after a pause).
        med = float(np.median(dbs))
        flag = "   ⚠ AGC onset blast" if med > 4.0 else "   (ok)"
        print(f"  speech onsets after pauses: {len(onsets)}"
              f"  median {med:+.1f} dB, worst {max(dbs):+.1f} dB{flag}")
        if med > 4.0:
            for tt, db in [o for o in onsets if o[1] > 4.0][:8]:
                print(f"      t={tt:6.1f}s  {db:+.1f} dB")
    else:
        print("  speech onsets after pauses: none found (continuous programme)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for p in sys.argv[1:]:
        review(p)
