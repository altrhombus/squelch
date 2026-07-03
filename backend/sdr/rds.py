"""
Pure Python RDS decoder.

Extracts PS, RadioText, PTY, and PI from the FM composite signal.

Pipeline:
  FM composite (float32, 240 kHz)
  → BPF 54-60 kHz    [isolate 57 kHz RDS subcarrier]
  → × 57 kHz carrier [mix to baseband; carrier = 3× pilot from Hilbert]
  → LPF 2.4 kHz      [isolate baseband BPSK]
  → resample → 19 kHz (exactly 16 samples per 1187.5 bps bit)
  → differential BPSK decisions
  → Manchester decode
  → syndrome-based block sync + CRC
  → group decode → PS / RadioText / PTY / PI

Call feed(composite, pilot_analytic) each DSP block. The callback is
invoked with a dict when any field changes.
"""

import logging
import time
import numpy as np
from scipy.signal import butter, sosfilt, resample_poly, hilbert
from typing import Callable, Optional

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


def _syndrome_matches(word26: int) -> Optional[str]:
    s = _syndrome(word26)
    for name, offset in _OFFSETS.items():
        if s == offset:
            return name
    return None


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

        # 19 kHz pilot extraction — must bandpass the pilot first, then cube
        # the analytic signal to get the 57 kHz carrier.  Passing hilbert of
        # the full composite (as was done before) gives the wrong carrier.
        self._pilot_sos = butter(4, [17_000, 21_000], 'bandpass', fs=_DEMOD_RATE, output='sos')
        self._pilot_zi  = _zero_zi(self._pilot_sos)

        # Subcarrier extraction (BPF 54-60 kHz at 240 kHz)
        self._rds_sos = butter(6, [54_000, 60_000], 'bandpass', fs=_DEMOD_RATE, output='sos')
        self._rds_zi  = _zero_zi(self._rds_sos)

        # Baseband LPF after mixing (2.4 kHz at 240 kHz)
        self._lp_sos = butter(4, 2_400, 'lowpass', fs=_DEMOD_RATE, output='sos')
        self._lp_zi  = _zero_zi(self._lp_sos)

        # Bit-stream buffer (at 19 kHz)
        self._sample_buf: list[float] = []
        self._bit_buf: list[int] = []     # 26-bit sliding window for syndrome check
        self._prev_bit = 0               # for differential decoding

        # Sync state
        self._synced  = False
        self._sync_offset = 0            # sample offset within SPS
        self._blocks: list[int] = []     # assembled 16-bit data words
        self._block_types: list[str] = []

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

        # 1. Extract 19 kHz pilot with BPF, then build the 57 kHz carrier.
        #    The carrier must be derived from hilbert(pilot_filtered), NOT
        #    from hilbert(composite) — the latter gives a noisy broadband
        #    analytic signal that produces the wrong carrier when cubed.
        pilot, self._pilot_zi = sosfilt(self._pilot_sos, c, zi=self._pilot_zi)
        pilot_a   = hilbert(pilot).astype(np.complex64)
        c57       = pilot_a ** 3                           # 57 kHz analytic
        c57_norm  = (c57 / (np.abs(c57) + 1e-10)).real.astype(np.float64)

        # 2. BPF around 57 kHz to isolate the RDS subcarrier
        rds_band, self._rds_zi = sosfilt(self._rds_sos, c, zi=self._rds_zi)

        # 3. Mix to baseband + LPF
        baseband = rds_band * c57_norm
        baseband, self._lp_zi = sosfilt(self._lp_sos, baseband, zi=self._lp_zi)

        # 4. Resample to 19 kHz (rational: 19/240)
        resampled = resample_poly(baseband, 19, 240).astype(np.float32)

        # 5. Accumulate samples and extract bits
        self._sample_buf.extend(resampled.tolist())
        self._extract_bits()

    # ------------------------------------------------------------------
    # Internal: bit extraction, sync, group decode
    # ------------------------------------------------------------------

    def _extract_bits(self):
        """
        Sample the baseband signal at the RDS bit rate and differential-decode.

        RDS uses differential BPSK — each bit is encoded as a phase change
        (1) or no change (0).  After coherent demodulation to baseband the
        signal is ±1 per bit period; differential decoding (XOR consecutive
        samples) recovers the data bits directly.

        There is NO additional Manchester/biphase layer in RDS.  The earlier
        _manchester_and_group step was wrong and was effectively halving the
        bit rate reaching the syndrome checker, which is why decoding was
        extremely rare (one group per several minutes instead of ~11/sec).
        """
        buf = self._sample_buf
        sps = _SPS   # exactly 16.0 = 19000 / 1187.5

        pos = 0.0
        while pos + sps <= len(buf):
            idx = int(pos + sps / 2)
            if idx < len(buf):
                raw = 1 if buf[idx] >= 0.0 else 0
                diff = raw ^ self._prev_bit
                self._prev_bit = raw
                self._push_bit(diff)
            pos += sps

        consumed = int(pos)
        self._sample_buf = buf[consumed:]
        # Guard against unbounded growth if processing falls behind
        if len(self._sample_buf) > 800:
            self._sample_buf = self._sample_buf[-400:]

    def _push_bit(self, bit: int):
        """Push one RDS bit into the 26-bit sliding window for sync/decode."""
        self._bit_buf.append(bit)
        if len(self._bit_buf) < 26:
            return
        if len(self._bit_buf) > 26:
            self._bit_buf.pop(0)

        word26 = 0
        for b in self._bit_buf:
            word26 = (word26 << 1) | b

        block_type = _syndrome_matches(word26)
        if block_type is None:
            return

        # Extract 16-bit data word
        data = (word26 >> 10) & 0xFFFF

        if not self._synced:
            if block_type == "A":
                self._synced = True
                self._blocks = [data]
                self._block_types = ["A"]
                self._bit_buf.clear()
        else:
            # Expected next block given the last received
            _next = {"A": ("B",), "B": ("C", "C'"), "C": ("D",), "C'": ("D",)}
            prev = self._block_types[-1] if self._block_types else None
            if prev and block_type not in _next.get(prev, ()):
                # Unexpected sequence — lose sync and start searching again
                self._synced = False
                self._blocks.clear()
                self._block_types.clear()
                return

            self._blocks.append(data)
            self._block_types.append(block_type)
            self._bit_buf.clear()

            if len(self._blocks) == 4:
                self._decode_group(self._blocks, self._block_types)
                self._blocks.clear()
                self._block_types.clear()
                # Stay synced after a good group — re-acquire only on failure.
                # Re-syncing every group throws away the alignment we just found
                # and wastes 26 bits searching each time.
                self._synced = True

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
