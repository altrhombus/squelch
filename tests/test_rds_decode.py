"""Bit-level and end-to-end tests for the RDS block decoder.

Bit-level tests drive _push_bit with synthesized 26-bit codewords to
verify burst error correction and position-tracked sync (alignment held
through CRC failures).

The end-to-end test synthesizes a physically faithful FM composite —
19 kHz pilot plus a biphase-coded, differentially-encoded RDS stream on
the 57 kHz subcarrier — and runs it through the full DSP front end
(carrier recovery, mixing, stateful resampling, symbol-timing
acquisition) in pipeline-sized blocks.
"""

import numpy as np

from backend.sdr.rds import (
    RdsDecoder,
    _BURST_ERRORS,
    _MAX_BAD_BLOCKS,
    _OFFSETS,
    _syndrome,
)

PI = 0x9552
PS_TEXT = "RDS TEST"


# ---------------------------------------------------------------------------
# Codeword construction
# ---------------------------------------------------------------------------

def codeword(data: int, offset_name: str) -> int:
    """26-bit block: 16 data bits + 10 check bits (CRC ⊕ offset word)."""
    crc = _syndrome(data << 10)
    return (data << 10) | (crc ^ _OFFSETS[offset_name])


def ps_group_words(pi: int, seg: int, two_chars: str) -> list[int]:
    """Group 0A carrying one PS segment."""
    b = (0 << 12) | seg
    d = (ord(two_chars[0]) << 8) | ord(two_chars[1])
    return [codeword(pi, "A"), codeword(b, "B"), codeword(0, "C"), codeword(d, "D")]


def ps_cycle_words(pi: int, text8: str) -> list[int]:
    words = []
    for seg in range(4):
        words += ps_group_words(pi, seg, text8[seg * 2: seg * 2 + 2])
    return words


def push_words(dec: RdsDecoder, words: list[int], flip: dict | None = None):
    """Feed codewords MSB-first into the bit-level state machine.
    flip maps word index → 26-bit XOR mask applied before pushing."""
    for i, w in enumerate(words):
        if flip and i in flip:
            w ^= flip[i]
        for k in range(25, -1, -1):
            dec._push_bit((w >> k) & 1)


def make_decoder(updates: list) -> RdsDecoder:
    return RdsDecoder(updates.append)


def ps_values(updates: list) -> list:
    return [u["ps"] for u in updates if "ps" in u]


# ---------------------------------------------------------------------------
# Burst correction table
# ---------------------------------------------------------------------------

def test_burst_table_corrects_all_listed_bursts():
    """syndrome is linear: for every burst e in the table, a corrupted
    codeword must decode back to the original data."""
    data = 0xA5C3
    for offset_name in ("A", "B", "C", "D"):
        cw = codeword(data, offset_name)
        for e in _BURST_ERRORS.values():
            s = _syndrome(cw ^ e)
            assert _BURST_ERRORS.get(s ^ _OFFSETS[offset_name]) == e


def test_burst_table_covers_single_and_double_bits():
    lengths = {e.bit_length() - (e & -e).bit_length() + 1 for e in _BURST_ERRORS.values()}
    assert lengths == {1, 2}
    # 26 single-bit + 25 adjacent-pair patterns
    assert len(_BURST_ERRORS) == 51


# ---------------------------------------------------------------------------
# Position-tracked sync + correction, bit level
# ---------------------------------------------------------------------------

def test_clean_stream_decodes_ps():
    updates = []
    dec = make_decoder(updates)
    push_words(dec, ps_cycle_words(PI, PS_TEXT) * 2)
    assert PS_TEXT in ps_values(updates)
    assert any(u.get("pi") == f"{PI:04X}" for u in updates)


def test_single_bit_error_is_corrected():
    updates = []
    dec = make_decoder(updates)
    words = ps_cycle_words(PI, PS_TEXT)
    # Flip one bit in block D of every group — detect-only decoding would
    # never assemble a PS at all.
    push_words(dec, words, flip={3: 1 << 7, 7: 1 << 20, 11: 1 << 0, 15: 1 << 13})
    assert PS_TEXT in ps_values(updates)


def test_two_bit_burst_is_corrected():
    updates = []
    dec = make_decoder(updates)
    words = ps_cycle_words(PI, PS_TEXT)
    push_words(dec, words, flip={5: 0b11 << 9})
    assert PS_TEXT in ps_values(updates)


def test_uncorrectable_block_drops_group_but_holds_sync():
    updates = []
    dec = make_decoder(updates)
    cycle = ps_cycle_words(PI, PS_TEXT)
    # 3-bit burst in block C of the second group: that group is lost.
    push_words(dec, cycle, flip={6: 0b111 << 4})
    assert dec._synced, "one bad block must not cost sync"
    assert PS_TEXT not in ps_values(updates)   # seg 1 was lost mid-run
    # The very next clean cycle decodes — no re-acquisition needed.
    push_words(dec, cycle)
    assert PS_TEXT in ps_values(updates)


def test_sustained_garbage_forces_resync():
    updates = []
    dec = make_decoder(updates)
    push_words(dec, ps_cycle_words(PI, PS_TEXT))
    assert dec._synced
    rng = np.random.default_rng(7)
    # Random words are overwhelmingly uncorrectable against the expected
    # offset; after _MAX_BAD_BLOCKS consecutive failures sync is dropped.
    for _ in range(_MAX_BAD_BLOCKS * 3):
        push_words(dec, [int(rng.integers(0, 1 << 26))])
        if not dec._synced:
            break
    assert not dec._synced


def test_search_requires_exact_match():
    """While unsynced, a corrupted block A must NOT establish sync (error
    correction is disabled during the search to prevent false sync)."""
    updates = []
    dec = make_decoder(updates)
    push_words(dec, [codeword(PI, "A") ^ (1 << 12)])
    assert not dec._synced
    push_words(dec, [codeword(PI, "A")])
    assert dec._synced


# ---------------------------------------------------------------------------
# End-to-end: composite waveform → decoded PS
# ---------------------------------------------------------------------------

_FS = 240_000
_BAUD = 1187.5


def make_composite(words: list[int], rds_amp: float = 0.06,
                   ppm: float = 0.0, noise: float = 0.0) -> np.ndarray:
    """FM composite: pilot + differentially-encoded biphase RDS at 57 kHz
    (3× pilot phase), plus an L+R audio tone for realism.

    ppm scales the pilot AND the bit clock together (they're locked at the
    transmitter), simulating a receiver clock error; noise adds white
    Gaussian noise across the whole composite.
    """
    scale = 1.0 + ppm * 1e-6
    baud = _BAUD * scale
    f_pilot = 19_000.0 * scale

    bits = []
    for w in words:
        bits += [(w >> k) & 1 for k in range(25, -1, -1)]
    # Differential encoding: e_k = e_{k-1} ⊕ d_k
    e = np.zeros(len(bits), dtype=np.int64)
    prev = 0
    for i, d in enumerate(bits):
        prev ^= d
        e[i] = prev

    n = int(len(bits) / baud * _FS)
    t = np.arange(n) / _FS
    tb = t * baud
    k = np.minimum(tb.astype(np.int64), len(bits) - 1)
    frac = tb - k
    # Biphase level coding: bit 1 → (+,−), bit 0 → (−,+)
    biphase = (2.0 * e[k] - 1.0) * np.where(frac < 0.5, 1.0, -1.0)

    theta = 2 * np.pi * f_pilot * t
    comp = (0.1 * np.cos(theta)
            + rds_amp * biphase * np.cos(3 * theta)
            + 0.4 * np.sin(2 * np.pi * 1_000 * t))
    if noise:
        comp = comp + np.random.default_rng(1).standard_normal(n) * noise
    return comp.astype(np.float32)


def feed_blocks(dec: RdsDecoder, comp: np.ndarray):
    block = 52_429   # same composite block size the FM pipeline produces
    for i in range(0, len(comp), block):
        dec.feed(comp[i:i + block])


def test_end_to_end_composite_decodes_ps_and_pi():
    updates = []
    dec = make_decoder(updates)
    # ~8 s of signal: the timing tracker starts at the worst possible phase
    # (mid-bit transition) and needs a few blocks of energy statistics to
    # snap to a lobe centre before groups start decoding.
    words = ps_cycle_words(PI, PS_TEXT) * 23
    feed_blocks(dec, make_composite(words))
    assert any(u.get("pi") == f"{PI:04X}" for u in updates)
    assert PS_TEXT in ps_values(updates)


def test_end_to_end_with_clock_error_and_noise():
    """Receiver ppm error drifts the bit clock through the symbol; the
    timing tracker must follow it (staying on one biphase lobe) while
    noise exercises the burst correction.  Measured headroom: ~57/34
    PS cycles decode under these conditions; assert far less."""
    updates = []
    dec = make_decoder(updates)
    words = ps_cycle_words(PI, PS_TEXT) * 34    # ~12 s
    feed_blocks(dec, make_composite(words, ppm=15, noise=0.05))
    assert ps_values(updates).count(PS_TEXT) >= 15
