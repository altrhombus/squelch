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
import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, resample_poly, hilbert
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


def _syndrome_matches(word26: int) -> Optional[str]:
    s = _syndrome(word26)
    for name, offset in _OFFSETS.items():
        if s == offset:
            return name
    return None


# ---------------------------------------------------------------------------
# Main decoder
# ---------------------------------------------------------------------------

def _zero_zi(sos):
    return np.zeros((sos.shape[0], 2), dtype=np.float64)


class RdsDecoder:
    """
    Stateful RDS decoder. Feed FM composite blocks; receive metadata callbacks.
    """

    def __init__(self, callback: Callable[[dict], None]):
        self._cb = callback

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
        self._pty: int                 = 0
        # PTY debounce: require the same value twice before reporting
        self._pty_candidate: int       = 0
        self._pty_seen: int            = 0

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
        self._pi   = a

        # PTY debounce: the same value must appear in two consecutive groups
        # before we accept it.  A single CRC false-positive can produce any
        # PTY code, so without this the badge cycles through random values.
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
            char0     = chr((d >> 8) & 0xFF)
            char1     = chr(d & 0xFF)
            self._ps_chars[seg] = (char0, char1)
            if len(self._ps_chars) == 4:
                ps = "".join(
                    self._ps_chars[s][0] + self._ps_chars[s][1]
                    for s in range(4)
                ).rstrip()
                update["ps"] = ps

        elif group_type == 2 and b0 == 0:   # Group 2A — RadioText (64 chars)
            seg    = b & 0xF
            flag   = (b >> 4) & 1
            if flag != self._rt_flag:
                self._rt_chars.clear()
                self._rt_flag = flag
            chars = [
                chr((c >> 8) & 0xFF), chr(c & 0xFF),
                chr((d >> 8) & 0xFF), chr(d & 0xFF),
            ]
            self._rt_chars[seg] = chars
            if len(self._rt_chars) == 16:
                rt = "".join(
                    "".join(self._rt_chars[s]) for s in range(16)
                ).rstrip()
                update["rt"] = rt

        elif group_type == 2 and b0 == 1:   # Group 2B — RadioText (32 chars)
            seg   = b & 0xF
            flag  = (b >> 4) & 1
            if flag != self._rt_flag:
                self._rt_chars.clear()
                self._rt_flag = flag
            chars = [chr((d >> 8) & 0xFF), chr(d & 0xFF)]
            self._rt_chars[seg] = chars
            if len(self._rt_chars) == 16:
                rt = "".join(
                    "".join(self._rt_chars[s]) for s in range(16)
                ).rstrip()
                update["rt"] = rt

        if update:
            logger.debug("RDS group decoded: type=%d%s %s", group_type, "B" if b0 else "A", update)
            self._cb(update)
