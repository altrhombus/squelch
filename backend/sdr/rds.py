"""
Pure Python RDS decoder.

Extracts PS, RadioText, PTY, and PI from the FM composite signal.

Pipeline:
  FM composite (float32, 240 kHz)
  → analytic pilot (heterodyne + stateful LPF) → ×3 phase → 57 kHz carrier
  → BPF 54-60 kHz    [isolate 57 kHz RDS subcarrier]
  → × carrier        [mix to baseband]
  → LPF 2.4 kHz      [isolate baseband biphase symbols]
  → stateful resample → 19 kHz (16 samples per 1187.5 bps bit)
  → per-bit sampling at an adaptively tracked phase → differential decode
  → position-tracked block sync + CRC with short-burst error correction
  → group decode → PS / RadioText / PTY / PI

Every stage carries state across feed() calls (filter zi, mixer phase,
resampler history, timing phase), so block boundaries are seamless — a
stateless resampler here used to corrupt bits at every ~218 ms boundary.

Call feed(composite) each DSP block. The callback is invoked with a dict
when any field changes.
"""

import logging
import time
import numpy as np
from scipy.signal import butter, sosfilt
from typing import Callable, Optional

from .dsp import PilotRecovery, StatefulResampler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RDS constants
# ---------------------------------------------------------------------------

_BAUD_RATE   = 1187.5
_RDS_RATE    = 19_000               # resample target (16 samples / bit)
_SPS         = _RDS_RATE / _BAUD_RATE   # ≈ 16 samples per symbol
_DEMOD_RATE  = 240_000

# RDS generator polynomial: x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
_GENERATOR = 0b10110111001

# Offset words (XORed with syndrome to identify block type)
_OFFSETS = {
    "A":  0b0011111100,
    "B":  0b0110011000,
    "C":  0b0101101000,
    "C'": 0b1101010000,
    "D":  0b0110110100,
}
_OFFSET_BY_SYNDROME = {v: k for k, v in _OFFSETS.items()}

# RT+ Application ID (IEC 62106 Annex A)
_RTP_APP_ID = 0x4BD7

# RBDS program type names (North America)
_PTY = {
    0:"",1:"News",2:"Information",3:"Sports",4:"Talk",5:"Rock",
    6:"Classic Rock",7:"Adult Hits",8:"Soft Rock",9:"Top 40",
    10:"Country",11:"Oldies",12:"Soft",13:"Nostalgia",14:"Jazz",
    15:"Classical",16:"R&B",17:"Soft R&B",18:"Foreign Language",
    19:"Religious Music",20:"Religious Talk",21:"Personality",
    22:"Public",23:"College",29:"Weather",30:"Emergency Test",31:"Emergency",
}


# ---------------------------------------------------------------------------
# CRC helper
# ---------------------------------------------------------------------------

def _syndrome(word26: int) -> int:
    """Compute the RDS syndrome for a 26-bit word."""
    reg = 0
    for i in range(25, -1, -1):
        bit = (word26 >> i) & 1
        if (reg >> 9) & 1:
            reg = ((reg << 1) | bit) ^ _GENERATOR
        else:
            reg = ((reg << 1) | bit)
        reg &= 0x3FF
    return reg


# ---------------------------------------------------------------------------
# Burst error correction — the (26,16) RDS code corrects bursts up to 5 bits,
# but every extra bit of correction capability raises the odds of "correcting"
# a garbage block into validity: a random syndrome hits the table with
# probability len(table)/1024.  Bursts ≤ 2 (51 patterns, ~5% false-accept on
# pure noise, absorbed by the PI/PTY/RT debounce layers upstream) fix the
# dominant real errors — isolated flips and short ISI bursts — and roughly
# double group throughput at threshold SNR versus detect-only.
# ---------------------------------------------------------------------------

_MAX_CORR_BURST = 2
_MAX_BAD_BLOCKS = 10   # consecutive CRC failures tolerated before re-syncing


def _build_burst_table() -> dict:
    """Map syndrome(e) → e for every burst error of length ≤ _MAX_CORR_BURST.

    The syndrome is linear over GF(2), so for a received word r = c ⊕ e:
    syndrome(r) = offset ⊕ syndrome(e).  Distinctness of the syndromes is
    guaranteed by the code's burst-correction capability (≤ 5)."""
    table: dict = {}
    for length in range(1, _MAX_CORR_BURST + 1):
        if length == 1:
            patterns = [1]
        else:
            # First and last burst bits set; interior bits free.
            patterns = [(1 << (length - 1)) | 1 | (mid << 1)
                        for mid in range(1 << (length - 2))]
        for pat in patterns:
            for pos in range(27 - length):
                e = pat << pos
                syn = _syndrome(e)
                assert syn not in table, "burst syndromes must be unique"
                table[syn] = e
    return table


_BURST_ERRORS = _build_burst_table()

# Expected block offsets by position within a group (A B C|C' D).
_EXPECTED_BY_POS = {0: ("A",), 1: ("B",), 2: ("C", "C'"), 3: ("D",)}


def _match_or_correct(word26: int, expected: tuple) -> tuple:
    """Return (block_type, data16, corrected) for a clean or burst-corrected
    block, or (None, None, False).  Exact matches win over corrections when
    the position admits two offsets (C vs C')."""
    s = _syndrome(word26)
    for name in expected:
        if s == _OFFSETS[name]:
            return name, (word26 >> 10) & 0xFFFF, False
    for name in expected:
        e = _BURST_ERRORS.get(s ^ _OFFSETS[name])
        if e is not None:
            return name, ((word26 ^ e) >> 10) & 0xFFFF, True
    return None, None, False


def _extract_rtp_tag(rt: str, start: int, length: int) -> Optional[str]:
    """
    Extract one RT+ tagged substring from the current RadioText string.

    Per IEC 62106 Annex A: start is the 0-indexed character position, and
    length is the number of *additional* characters after start, so the
    actual extracted window is rt[start : start + length + 1].
    Returns the stripped result, or None if the window is out of range.
    """
    end = start + length + 1
    if end > len(rt):
        return None
    return rt[start:end].strip("\x00\x20\x0d\x0a") or None


# ---------------------------------------------------------------------------
# Main decoder
# ---------------------------------------------------------------------------

# RDS basic character set, codes 0x80-0xFF — EN 50067:1998 Annex E
# (transcribed from redsea's codetable_G0, the reference RDS decoder).
# Codes 0x20-0x7E are decoded as plain ASCII: strictly the EBU table differs
# in a few spots (0x24→¤, 0x5E→―, 0x60→‖, 0x7E→¯), but real-world encoders
# overwhelmingly send ASCII there and the strict mapping would mangle '$'.
_G0_HIGH = (
    "áàéèíìóòúùÑÇŞβ¡Ĳ"
    "âäêëîïôöûüñçşǧıĳ"
    "ªα©‰Ǧěňőπ€£$←↑→↓"
    "º¹²³±İńűµ¿÷°¼½¾§"
    "ÁÀÉÈÍÌÓÒÚÙŘČŠŽÐĿ"
    "ÂÄÊËÎÏÔÖÛÜřčšžđŀ"
    "ÃÅÆŒŷÝÕØÞŊŔĆŚŹŦð"
    "ãåæœŵýõøþŋŕćśźŧ "
)


def _rds_char(code: int) -> Optional[str]:
    """Decode one RDS text byte; None = control/invalid (likely bit error)."""
    if code == 0x0D:
        return "\r"          # message terminator
    if code == 0x0A:
        return " "           # line break — render single-line
    if 0x20 <= code < 0x7F:
        return chr(code)
    if code >= 0x80:
        return _G0_HIGH[code - 0x80]
    return None


# Partial RadioText display: if the buffer is still incomplete after this
# long with at least this many segments, emit a gap-padded version so weak
# signals show *something* while the rest fills in.  Partial emissions are
# flagged and never reach history or the iTunes lookup.
_RT_PARTIAL_MIN_SEGS   = 12
_RT_PARTIAL_AFTER_SECS = 15.0


def _rt_emit(rt_chars: dict, n_segs: int) -> str:
    """Join received segments (missing ones as spaces) and honor the 0x0D
    terminator — everything after it is padding."""
    seg_len = len(next(iter(rt_chars.values())))
    full = "".join(
        "".join(rt_chars.get(s, [" "] * seg_len)) for s in range(n_segs)
    )
    return full.split("\r")[0].rstrip()


def _zero_zi(sos):
    return np.zeros((sos.shape[0], 2), dtype=np.float64)


class RdsDecoder:
    """
    Stateful RDS decoder. Feed FM composite blocks; receive metadata callbacks.
    """

    def __init__(self, callback: Callable[[dict], None], clock=time.monotonic):
        self._cb = callback
        self._clock = clock

        # Analytic 19 kHz pilot for carrier synthesis (heterodyne + stateful
        # LPF — see PilotRecovery).  Must derive from the pilot, not from a
        # hilbert of the full composite, which gives the wrong carrier when
        # cubed.  Phase-continuous across feed() calls.
        self._pilot_rec = PilotRecovery(_DEMOD_RATE)

        # Subcarrier extraction (BPF 54-60 kHz at 240 kHz)
        self._rds_sos = butter(6, [54_000, 60_000], 'bandpass', fs=_DEMOD_RATE, output='sos')
        self._rds_zi  = _zero_zi(self._rds_sos)

        # Baseband LPF after mixing (2.4 kHz at 240 kHz)
        self._lp_sos = butter(4, 2_400, 'lowpass', fs=_DEMOD_RATE, output='sos')
        self._lp_zi  = _zero_zi(self._lp_sos)

        # Stateful 240 kHz → 19 kHz resampler.  A per-block resample_poly
        # restarted its FIR at every DSP block boundary, corrupting 1-2 bits
        # ~4.6 times a second regardless of signal quality.
        self._resamp = StatefulResampler(19, 240)

        # Bit-stream buffer (at 19 kHz)
        self._sample_buf: list[float] = []
        self._bit_buf: list[int] = []     # 26-bit sliding window for syndrome check
        self._prev_bit = 0               # for differential decoding

        # Symbol timing tracker (see _extract_bits).  Mean |amplitude| per
        # intra-bit phase bin; the sampling phase is steered toward the
        # energy maximum — snap while unsynced, slow slew while synced.
        self._samp_phase     = 8.0              # sampling offset within a bit
        self._buf_abs        = 0                # absolute index of _sample_buf[0]
        self._abs_out        = 0                # absolute 19 kHz samples produced
        self._phase_energy   = np.zeros(16)     # EMA of |amp| per phase bin
        self._energy_updates = 0

        # Sync state.  While synced, the bit position within the 104-bit
        # group cycle is tracked explicitly so a CRC-failed block blanks its
        # slot instead of costing a full re-acquisition.
        self._synced  = False
        self._block_pos = 0              # expected block within group (0-3)
        self._bad_blocks = 0             # consecutive CRC failures while synced
        self._blocks: list = []          # assembled 16-bit data words (None = lost)
        self._block_types: list = []

        # RDS fields
        self._pi: Optional[int]     = None
        self._ps_chars: dict[int, tuple[str,str]] = {}  # segment → (char0, char1)
        self._rt_chars: dict[int, list[str]] = {}       # segment → chars (2A: 4, 2B: 2)
        self._rt_flag: Optional[int]  = None
        # Extended-char segments (accented names) need the same value twice —
        # a bit error flipping a char's high bit looks identical to a legit
        # accent, and voting doesn't exist on the RT path.
        self._rt_pending: dict[int, list[str]] = {}
        # A/B flag flips only when the message changes; a single corrupted
        # flag bit must not wipe 16 accumulated segments (spurious resets
        # dominated RT latency on weak signals).
        self._rt_flag_flips: int = 0
        self._rt_held: Optional[tuple] = None   # group held while confirming a flip
        self._rt_first_seg_t: Optional[float] = None   # accumulation start (partial timer)
        self._rt_last_emit: Optional[tuple] = None     # (text, partial) dedupe
        self._pty: int                 = 0
        # PTY debounce: require the same value twice before reporting
        self._pty_candidate: int       = 0
        self._pty_seen: int            = 0
        # PI debounce: a station's PI code never changes; varying PI = bit errors
        self._pi_candidate: Optional[int] = None
        self._pi_seen: int                = 0
        # RT+ (RadioText Plus) — IEC 62106 Annex A
        # Group 3A announces which ODA group type carries RT+ for this station.
        self._rtp_group_type: Optional[int] = None  # 0-15
        self._rtp_group_ver:  int            = 0     # 0 = A, 1 = B

    # ------------------------------------------------------------------

    def feed(self, composite: np.ndarray):
        """
        Feed one DSP block of FM composite (float32, 240 kHz).
        Pilot is extracted internally so the caller doesn't need to manage it.
        """
        c = composite.astype(np.float64)

        # 1. Analytic pilot A·e^{jθ}; cubing the unit phasor gives the
        #    phase-locked 57 kHz carrier cos(3θ).
        pilot_a  = self._pilot_rec.process(c)
        u        = pilot_a / (np.abs(pilot_a) + 1e-10)
        c57_norm = (u ** 3).real

        # 2. BPF around 57 kHz to isolate the RDS subcarrier
        rds_band, self._rds_zi = sosfilt(self._rds_sos, c, zi=self._rds_zi)

        # 3. Mix to baseband + LPF
        baseband = rds_band * c57_norm
        baseband, self._lp_zi = sosfilt(self._lp_sos, baseband, zi=self._lp_zi)

        # 4. Resample to 19 kHz (rational: 19/240) — stateful, so block
        #    boundaries carry no filter edge transients into the bit stream.
        resampled = self._resamp.process(baseband).astype(np.float32)

        # 5. Symbol-timing energy: mean |amplitude| in each of the 16
        #    intra-bit phase bins, tracked on an absolute sample grid so the
        #    estimate is consistent across blocks of arbitrary length.
        m = len(resampled) // 16
        if m >= 4:
            e = np.abs(resampled[:m * 16]).reshape(m, 16).mean(axis=0)
            s = self._abs_out % 16
            e_abs = np.empty(16)
            e_abs[(s + np.arange(16)) % 16] = e
            if self._energy_updates == 0:
                self._phase_energy = e_abs
            else:
                self._phase_energy += 0.25 * (e_abs - self._phase_energy)
            self._energy_updates += 1
        self._abs_out += len(resampled)

        # 6. Accumulate samples and extract bits
        self._sample_buf.extend(resampled.tolist())
        self._extract_bits()

    # ------------------------------------------------------------------
    # Internal: bit extraction, sync, group decode
    # ------------------------------------------------------------------

    def _extract_bits(self):
        """
        Sample the baseband signal once per bit and differential-decode.

        RDS transmits the differentially-encoded bit stream as biphase
        (Manchester-like) symbols at 1187.5 baud (EN 50067 §5.1): each bit
        occupies two half-bit lobes of opposite sign.  Sampling once per
        bit period at a CONSISTENT phase inside either lobe yields the
        encoded stream (or its complement — a constant inversion cancels
        in the differential XOR), so no explicit biphase merge is needed.
        What does matter is the sampling phase:

        - landing near the mid-bit lobe transition makes every decision
          noise-dominated (and a fixed arbitrary phase could land there
          permanently, depending on when the decoder started);
        - the SDR clock's ppm error makes the true bit period ≠ exactly
          16 output samples, so any fixed phase slowly drifts through the
          symbol and periodically slips a bit.

        The tracker steers the sampling phase toward the maximum of the
        per-phase |amplitude| profile measured in feed(): a snap while
        unsynced (fast acquisition), a bounded slew while synced (ppm
        tracking without spurious bit slips).
        """
        buf = self._sample_buf
        sps = int(_SPS)   # exactly 16 = 19000 / 1187.5

        if self._energy_updates >= 3:
            # A biphase bit has TWO energy maxima — one per half-lobe,
            # 8 samples apart and near-equal — so the profile is folded
            # mod 8 and the phase steered to the NEAREST lobe centre.
            # Tracking the raw 16-bin argmax made the tracker hunt between
            # the two lobes under clock drift, mixing sample polarity and
            # corrupting most groups.  Staying on one lobe keeps polarity
            # consistent (a constant inversion cancels in the differential
            # XOR); lobe changes only happen via explicit phase wraps —
            # a single recoverable bit slip.
            e8 = self._phase_energy[:8] + self._phase_energy[8:]
            best = int(np.argmax(e8))
            cur = (self._buf_abs + int(self._samp_phase)) % 8
            err = (best - cur + 4) % 8 - 4           # signed, in [-4, 4)
            # Snap during acquisition (unsynced, or synced-on-noise with
            # accumulating CRC failures); bounded slew while genuinely
            # locked so ppm drift is tracked without spurious bit slips.
            if abs(err) > 1.5 and (not self._synced or self._bad_blocks >= 3):
                step = float(err)
            else:
                step = float(np.clip(0.2 * err, -0.5, 0.5))
            self._samp_phase += step
            if self._samp_phase >= 16.0:              # deliberate bit slip;
                self._samp_phase -= 16.0              # sync recovers
            elif self._samp_phase < 0.0:
                self._samp_phase += 16.0

        pos = 0
        while pos + sps <= len(buf):
            idx = pos + int(self._samp_phase)
            raw = 1 if buf[idx] >= 0.0 else 0
            diff = raw ^ self._prev_bit
            self._prev_bit = raw
            self._push_bit(diff)
            pos += sps

        self._sample_buf = buf[pos:]
        self._buf_abs += pos
        # Guard against unbounded growth if processing falls behind
        if len(self._sample_buf) > 800:
            dropped = len(self._sample_buf) - 400
            self._sample_buf = self._sample_buf[-400:]
            self._buf_abs += dropped

    def _push_bit(self, bit: int):
        """Push one RDS bit into the sync/decode state machine.

        Unsynced: 26-bit sliding-window search for an exact block-A
        syndrome (correction disabled while searching — a burst-corrected
        match is far too weak an anchor and would cause false sync on
        noise).

        Synced: consume exactly 26 bits per block and hold that alignment
        even through CRC failures.  A failed block blanks its slot in the
        group (the group is discarded) instead of throwing away the bit
        alignment — the old lose-sync-on-any-error behaviour cost the
        group PLUS a ~13-bit average re-search PLUS the next partial
        group for every single bit error.  Sync is only re-acquired after
        _MAX_BAD_BLOCKS consecutive failures (a real fade or a genuine
        slip, not an isolated corrupt block).
        """
        self._bit_buf.append(bit)

        if not self._synced:
            if len(self._bit_buf) < 26:
                return
            if len(self._bit_buf) > 26:
                self._bit_buf.pop(0)
            word26 = 0
            for b in self._bit_buf:
                word26 = (word26 << 1) | b
            if _syndrome(word26) == _OFFSETS["A"]:
                self._synced = True
                self._blocks = [(word26 >> 10) & 0xFFFF]
                self._block_types = ["A"]
                self._block_pos = 1
                self._bad_blocks = 0
                self._bit_buf.clear()
            return

        if len(self._bit_buf) < 26:
            return
        word26 = 0
        for b in self._bit_buf:
            word26 = (word26 << 1) | b
        self._bit_buf.clear()

        block_type, data, corrected = _match_or_correct(
            word26, _EXPECTED_BY_POS[self._block_pos])
        if block_type is None:
            self._bad_blocks += 1
            if self._bad_blocks >= _MAX_BAD_BLOCKS:
                self._synced = False
                self._blocks.clear()
                self._block_types.clear()
                return
            self._blocks.append(None)
            self._block_types.append(None)
        else:
            # Only a CLEAN block fully resets the failure counter.  A
            # corrected block merely decrements it: garbage bits get
            # "corrected" into validity ~5-10% of the time, and letting
            # those coincidences reset the counter livelocked a false sync
            # (synced on noise, never re-acquiring).  A genuine lock
            # produces clean blocks often enough to keep the counter down.
            if corrected:
                self._bad_blocks = max(0, self._bad_blocks - 1)
            else:
                self._bad_blocks = 0
            self._blocks.append(data)
            self._block_types.append(block_type)

        self._block_pos = (self._block_pos + 1) % 4
        if self._block_pos == 0:
            if all(b is not None for b in self._blocks):
                self._decode_group(self._blocks, self._block_types)
            self._blocks.clear()
            self._block_types.clear()

    def _decode_group(self, blocks: list[int], types: list[str]):
        if len(blocks) < 4:
            return

        a, b, c, d = blocks
        group_type = (b >> 12) & 0xF
        b0         = (b >> 11) & 1
        pty_raw    = (b >> 5) & 0x1F
        pi_raw     = a

        # PI debounce: a station's PI code never changes; if it varies between
        # groups the data bits are corrupted.  Require the same value twice.
        if pi_raw == self._pi_candidate:
            self._pi_seen += 1
        else:
            self._pi_candidate = pi_raw
            self._pi_seen = 1
        if self._pi_seen >= 2:
            self._pi = pi_raw

        # PTY debounce: same reason — require same value twice.
        if pty_raw == self._pty_candidate:
            self._pty_seen += 1
        else:
            self._pty_candidate = pty_raw
            self._pty_seen = 1
        if self._pty_seen >= 2:
            self._pty = pty_raw

        update: dict = {}

        if self._pi is not None:
            update["pi"] = f"{self._pi:04X}"

        pty_name = _PTY.get(self._pty, "")
        if pty_name:
            update["pty"] = pty_name

        if group_type == 0:          # Group 0A/0B — PS name
            seg       = b & 0x3
            char0     = _rds_char((d >> 8) & 0xFF)
            char1     = _rds_char(d & 0xFF)
            # Segments must arrive in transmission order (0,1,2,3) with no
            # gaps, and the buffer is cleared after every emission.  Dynamic-PS
            # stations replace the whole message every ~1 s; the old
            # accumulate-and-overwrite approach emitted a hybrid of 3 stale +
            # 1 fresh segment on every group after the first fill, flooding
            # the downstream page reassembler with frankenpages.
            # Control codes / invalid bytes are bit errors — restart the run.
            if char0 is None or char1 is None or "\r" in (char0, char1):
                self._ps_chars.clear()
            elif seg == 0:
                self._ps_chars = {0: (char0, char1)}
            elif len(self._ps_chars) == seg:
                self._ps_chars[seg] = (char0, char1)
            else:
                self._ps_chars.clear()   # out-of-order — restart the run
            if len(self._ps_chars) == 4:
                # Deliver the raw 8 characters unstripped — stations that page
                # song text through PS rely on the space padding for word
                # boundaries when the pages are reassembled downstream.
                ps = "".join(
                    self._ps_chars[s][0] + self._ps_chars[s][1]
                    for s in range(4)
                )
                self._ps_chars.clear()
                if ps.strip():
                    update["ps"] = ps

        elif group_type == 2 and b0 == 0:   # Group 2A — RadioText (64 chars)
            seg   = b & 0xF
            flag  = (b >> 4) & 1
            bytes_ = [(c >> 8) & 0xFF, c & 0xFF, (d >> 8) & 0xFF, d & 0xFF]
            self._handle_rt(seg, bytes_, flag, update)

        elif group_type == 2 and b0 == 1:   # Group 2B — RadioText (32 chars)
            seg   = b & 0xF
            flag  = (b >> 4) & 1
            self._handle_rt(seg, [(d >> 8) & 0xFF, d & 0xFF], flag, update)

        elif group_type == 3 and b0 == 0:  # Group 3A — ODA application announcement
            # Block C = 16-bit Application ID
            # Block B bits [4:1] = ODA group type number (0-15)
            # Block B bit  [0]   = ODA version (0 = A, 1 = B)
            if c == _RTP_APP_ID:
                self._rtp_group_type = (b >> 1) & 0xF
                self._rtp_group_ver  =  b & 0x1
                logger.info("RDS RT+ active on group %d%s",
                            self._rtp_group_type, "B" if self._rtp_group_ver else "A")

        elif (self._rtp_group_type is not None
              and group_type == self._rtp_group_type
              and b0 == self._rtp_group_ver):  # RT+ ODA data group
            # Bit layout per IEC 62106 Annex A, Table A.3:
            #
            # Block B [4]    item_toggle  — flips each time a new item starts
            # Block B [3]    item_running — 1 while an item (song) is playing
            # Block B [2:0]  content_type_1 [5:3]   (high 3 bits of ct1)
            # Block C [15:13] content_type_1 [2:0]  (low  3 bits of ct1)
            # Block C [12:7]  start_1 [5:0]          0-indexed char position
            # Block C [6:1]   length_1 [5:0]          actual length = value + 1
            # Block C [0]     content_type_2 [5]     (MSB of ct2)
            # Block D [15:11] content_type_2 [4:0]   (low 5 bits of ct2)
            # Block D [10:5]  start_2 [5:0]
            # Block D [4:0]   length_2 [4:0]          actual length = value + 1
            item_running = (b >> 3) & 0x1
            if item_running and self._rt_chars:
                ct1 = ((b & 0x7) << 3) | ((c >> 13) & 0x7)
                st1 = (c >> 7) & 0x3F
                ln1 = (c >> 1) & 0x3F
                ct2 = ((c & 0x1) << 5) | ((d >> 11) & 0x1F)
                st2 = (d >> 5) & 0x3F
                ln2 =  d & 0x1F

                # Reconstruct the current RadioText from whatever segments
                # have been received; use spaces for any gap segments.
                max_seg = max(self._rt_chars)
                chars_per_seg = len(next(iter(self._rt_chars.values())))
                rt_now = "".join(
                    "".join(self._rt_chars.get(s, ["\x20"] * chars_per_seg))
                    for s in range(max_seg + 1)
                )

                for ct, st, ln in ((ct1, st1, ln1), (ct2, st2, ln2)):
                    if ct == 1:    # ITEM.TITLE
                        v = _extract_rtp_tag(rt_now, st, ln)
                        if v:
                            update["rtp_title"] = v
                    elif ct == 4:  # ITEM.ARTIST
                        v = _extract_rtp_tag(rt_now, st, ln)
                        if v:
                            update["rtp_artist"] = v

                if "rtp_title" in update or "rtp_artist" in update:
                    logger.info("RDS RT+ tags: %s",
                                {k: update[k] for k in ("rtp_title", "rtp_artist")
                                 if k in update})

        if update:
            logger.debug("RDS group decoded: type=%d%s %s", group_type, "B" if b0 else "A", update)
            self._cb(update)

    def _handle_rt(self, seg: int, byte_vals: list, flag: int, update: dict):
        """Shared RadioText segment ingest for groups 2A and 2B."""
        if self._rt_flag is None:
            self._rt_flag = flag
        if flag != self._rt_flag:
            # The A/B flag flips only when the message changes.  Require two
            # consecutive groups with the new flag before wiping the buffer —
            # a single corrupted flag bit previously reset all accumulated
            # segments, which dominated RT latency on weak signals.  The
            # first new-flag group is held and replayed on confirmation so a
            # genuine change loses nothing.
            self._rt_flag_flips += 1
            if self._rt_flag_flips < 2:
                self._rt_held = (seg, list(byte_vals))
                return
            self._rt_chars.clear()
            self._rt_pending.clear()
            self._rt_first_seg_t = None
            self._rt_last_emit = None
            self._rt_flag = flag
            held, self._rt_held = self._rt_held, None
            if held:
                self._store_rt_segment(*held)
        else:
            self._rt_held = None
        self._rt_flag_flips = 0

        self._store_rt_segment(seg, byte_vals)
        self._maybe_emit_rt(update)

    def _maybe_emit_rt(self, update: dict):
        """Tiered emission:
        1. all 16 segments → complete (confident)
        2. every segment up to a 0x0D terminator → complete (confident);
           segments beyond the terminator are padding, no need to wait
        3. still incomplete after a while → partial, gaps as spaces,
           flagged rt_partial (display-only downstream)
        """
        n = len(self._rt_chars)
        if n == 0:
            return
        rt, partial = None, False
        if n == 16:
            rt = _rt_emit(self._rt_chars, 16)
        else:
            term = min((s for s, ch in self._rt_chars.items() if "\r" in ch),
                       default=None)
            if term is not None and all(s in self._rt_chars for s in range(term + 1)):
                rt = _rt_emit(self._rt_chars, term + 1)
            elif (n >= _RT_PARTIAL_MIN_SEGS
                  and self._rt_first_seg_t is not None
                  and self._clock() - self._rt_first_seg_t > _RT_PARTIAL_AFTER_SECS):
                rt = _rt_emit(self._rt_chars, 16)
                partial = True
        # Dedupe on (text, partial) so the partial→complete transition of
        # identical text still reaches the metadata layer as an upgrade.
        if rt is not None and (rt, partial) != self._rt_last_emit:
            self._rt_last_emit = (rt, partial)
            update["rt"] = rt
            update["rt_partial"] = partial

    def _store_rt_segment(self, seg: int, byte_vals: list):
        chars = [_rds_char(v) for v in byte_vals]
        if any(ch is None for ch in chars):
            return   # bit error — this segment comes around again

        if any(v >= 0x80 for v in byte_vals):
            # Extended chars (accented names) are legit, but a bit error on
            # an ASCII char's high bit looks identical — require the same
            # segment content twice before accepting it.
            if self._rt_pending.get(seg) != chars:
                self._rt_pending[seg] = chars
                return
        if self._rt_first_seg_t is None:
            self._rt_first_seg_t = self._clock()
        self._rt_chars[seg] = chars
