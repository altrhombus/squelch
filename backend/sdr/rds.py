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

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, resample_poly
from typing import Callable, Optional


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

        # Subcarrier extraction (BPF 54-60 kHz at 240 kHz)
        self._rds_sos = butter(6, [54_000, 60_000], 'bandpass', fs=_DEMOD_RATE, output='sos')
        self._rds_zi  = _zero_zi(self._rds_sos)

        # Baseband LPF after mixing (2.4 kHz at 240 kHz)
        self._lp_sos = butter(4, 2_400, 'lowpass', fs=_DEMOD_RATE, output='sos')
        self._lp_zi  = _zero_zi(self._lp_sos)

        # Bit-stream buffer (at 19 kHz)
        self._sample_buf: list[float] = []
        self._bit_buf: list[int] = []     # raw BPSK bits
        self._diff_buf: list[int] = []    # differential decoded
        self._prev_bit = 0

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

    # ------------------------------------------------------------------

    def feed(self, composite: np.ndarray, pilot_analytic: np.ndarray):
        """
        Feed one DSP block of FM composite (float32, 240 kHz) and the
        corresponding pilot analytic signal (complex64, 240 kHz).
        """
        # 1. BPF around 57 kHz
        rds_band, self._rds_zi = sosfilt(self._rds_sos, composite.astype(np.float64), zi=self._rds_zi)

        # 2. Generate 57 kHz carrier: 3× pilot phase
        carrier57 = (pilot_analytic.astype(np.complex64) ** 3)
        mag = np.abs(carrier57) + 1e-10
        carrier57_norm = (carrier57 / mag).real.astype(np.float64)

        # 3. Mix to baseband + LPF
        baseband = rds_band * carrier57_norm
        baseband, self._lp_zi = sosfilt(self._lp_sos, baseband, zi=self._lp_zi)

        # 4. Resample to _RDS_RATE (19 kHz)
        n_in  = len(baseband)
        # rational approximation: 19000/240000 = 19/240
        resampled = resample_poly(baseband, 19, 240).astype(np.float32)

        # 5. Accumulate samples and extract bits
        self._sample_buf.extend(resampled.tolist())
        self._extract_bits()

    # ------------------------------------------------------------------
    # Internal: bit extraction, sync, group decode
    # ------------------------------------------------------------------

    def _extract_bits(self):
        """Clock-recover bits from the sample buffer using zero-crossing timing."""
        buf = self._sample_buf
        sps = _SPS  # ≈ 16

        # Coarse clock: step through buffer taking one sample per bit period
        pos = 0.0
        new_bits: list[int] = []

        while pos + sps <= len(buf):
            # Sample at the current clock position
            idx = int(pos + sps / 2)
            if idx < len(buf):
                new_bits.append(1 if buf[idx] >= 0.0 else 0)
            pos += sps

        # Consume the used samples
        consumed = int(pos)
        self._sample_buf = buf[consumed:]

        if not new_bits:
            return

        # Differential decode
        for bit in new_bits:
            diff = bit ^ self._prev_bit
            self._prev_bit = bit
            self._diff_buf.append(diff)

        self._manchester_and_group()

    def _manchester_and_group(self):
        """Manchester decode diff_buf → bits, then do block sync + CRC."""
        db = self._diff_buf
        rds_bits: list[int] = []

        i = 0
        while i + 1 < len(db):
            b0, b1 = db[i], db[i+1]
            if b0 == 0 and b1 == 1:
                rds_bits.append(0)
                i += 2
            elif b0 == 1 and b1 == 0:
                rds_bits.append(1)
                i += 2
            else:
                # Manchester violation — resync: skip one sample
                i += 1

        self._diff_buf = db[i * 2 if i * 2 <= len(db) else len(db):]
        # Trim diff_buf to avoid unbounded growth
        if len(self._diff_buf) > 400:
            self._diff_buf = self._diff_buf[-200:]

        for bit in rds_bits:
            self._push_bit(bit)

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
            expected = {"A": "B", "B": "C", "C": "D", "C'": "D"}
            prev = self._block_types[-1] if self._block_types else None
            if block_type not in ("A", "B", "C", "C'", "D"):
                self._synced = False
                self._blocks.clear()
                self._block_types.clear()
                return

            self._blocks.append(data)
            self._block_types.append(block_type)
            self._bit_buf.clear()

            if len(self._blocks) == 4:
                self._decode_group(self._blocks, self._block_types)
                # After a complete group, start fresh looking for next A
                self._blocks.clear()
                self._block_types.clear()
                self._synced = False  # re-acquire sync each group (conservative)

    def _decode_group(self, blocks: list[int], types: list[str]):
        if len(blocks) < 4:
            return

        a, b, c, d = blocks
        group_type = (b >> 12) & 0xF
        b0         = (b >> 11) & 1
        self._pty  = (b >> 5) & 0x1F
        self._pi   = a

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
            self._cb(update)
