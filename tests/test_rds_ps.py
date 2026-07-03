"""PS segment-ordering tests for the RDS group decoder.

Drives _decode_group directly with synthetic group-0A blocks. The scenario
mirrors a real dynamic-PS station observed over the air: pages replaced
every ~1 s, where the old accumulate-and-overwrite logic emitted hybrid
'frankenpages' (3 stale segments + 1 fresh) on every group.
"""

from backend.sdr.rds import RdsDecoder

PI = 0x9552


def make_decoder(updates: list) -> RdsDecoder:
    return RdsDecoder(updates.append)


def group0(seg: int, two_chars: str):
    b = (0 << 12) | seg          # group 0A, PS segment address
    d = (ord(two_chars[0]) << 8) | ord(two_chars[1])
    return [PI, b, 0, d]


def feed_segment(dec: RdsDecoder, seg: int, two_chars: str):
    dec._decode_group(group0(seg, two_chars), ["A", "B", "C", "D"])


def feed_page(dec: RdsDecoder, text8: str):
    assert len(text8) == 8
    for seg in range(4):
        feed_segment(dec, seg, text8[seg * 2:seg * 2 + 2])


def ps_values(updates: list) -> list:
    return [u["ps"] for u in updates if "ps" in u]


# ---------------------------------------------------------------------------

def test_in_order_segments_emit_full_page():
    updates = []
    dec = make_decoder(updates)
    feed_page(dec, "The Payo")
    assert ps_values(updates) == ["The Payo"]


def test_no_hybrid_emissions_between_pages():
    """The frankenpage bug: after a full page, each subsequent segment must
    NOT emit a 3-old+1-new hybrid; only complete in-order runs emit."""
    updates = []
    dec = make_decoder(updates)
    feed_page(dec, "The Payo")
    feed_page(dec, "ff - Fat")
    feed_page(dec, "her John")
    feed_page(dec, " Misty  ")
    assert ps_values(updates) == ["The Payo", "ff - Fat", "her John", " Misty  "]


def test_page_change_mid_run_restarts_cleanly():
    updates = []
    dec = make_decoder(updates)
    # Page A aborted after two segments, then page B transmitted fully
    feed_segment(dec, 0, "Th")
    feed_segment(dec, 1, "e ")
    feed_page(dec, "ff - Fat")
    assert ps_values(updates) == ["ff - Fat"]


def test_out_of_order_segment_discards_run():
    updates = []
    dec = make_decoder(updates)
    feed_segment(dec, 0, "Th")
    feed_segment(dec, 2, "Pa")      # gap — segment 1 missed
    feed_segment(dec, 3, "yo")
    assert ps_values(updates) == []


def test_nonprintable_page_rejected():
    updates = []
    dec = make_decoder(updates)
    feed_segment(dec, 0, "Th")
    feed_segment(dec, 1, "e\x01")   # corrupted char
    feed_segment(dec, 2, "Pa")
    feed_segment(dec, 3, "yo")
    assert ps_values(updates) == []


# ---------------------------------------------------------------------------
# RadioText corruption guard (group 2A)
# ---------------------------------------------------------------------------

def group2a(seg: int, four_chars: str, flag: int = 0):
    b = (2 << 12) | (flag << 4) | seg
    c = (ord(four_chars[0]) << 8) | ord(four_chars[1])
    d = (ord(four_chars[2]) << 8) | ord(four_chars[3])
    return [PI, b, c, d]


def feed_rt(dec: RdsDecoder, text: str):
    """Feed a full 64-char RadioText (padded) as 16 4-char segments."""
    padded = text.ljust(64)
    for seg in range(16):
        dec._decode_group(group2a(seg, padded[seg * 4:seg * 4 + 4]),
                          ["A", "B", "C", "D"])


def rt_values(updates: list) -> list:
    return [u["rt"] for u in updates if "rt" in u]


def test_clean_radiotext_emitted():
    updates = []
    dec = make_decoder(updates)
    feed_rt(dec, "Nobody owns us but you!")
    assert rt_values(updates)[-1] == "Nobody owns us but you!"


def test_corrupt_rt_segment_rejected_until_reheard():
    """Bit-error segments (observed live as '\\x86ãWM' flashing in the UI)
    must not reach the metadata layer; the clean re-transmission fills the
    slot on the next RT cycle."""
    updates = []
    dec = make_decoder(updates)
    # First pass: segment 3 corrupted
    padded = "Nobody owns us but you!".ljust(64)
    for seg in range(16):
        chunk = padded[seg * 4:seg * 4 + 4] if seg != 3 else "y\x86\xe3W"
        dec._decode_group(group2a(seg, chunk), ["A", "B", "C", "D"])
    assert rt_values(updates) == []          # incomplete — corrupt seg dropped

    # Second pass: clean
    feed_rt(dec, "Nobody owns us but you!")
    assert rt_values(updates)[-1] == "Nobody owns us but you!"
    assert all("\x86" not in rt for rt in rt_values(updates))


def test_rt_terminator_truncates():
    updates = []
    dec = make_decoder(updates)
    feed_rt(dec, "Short message\rGARBAGE AFTER TERMINATOR")
    assert rt_values(updates)[-1] == "Short message"


# ---------------------------------------------------------------------------
# RDS extended charset (EN 50067:1998 Annex E)
# ---------------------------------------------------------------------------

def group0_bytes(seg: int, b0: int, b1: int):
    b = (0 << 12) | seg
    return [PI, b, 0, (b0 << 8) | b1]


def test_ps_decodes_accented_chars():
    """'Mötley C' — ö is RDS code 0x97, previously rejected as corruption,
    which stalled reassembly for any accented artist."""
    updates = []
    dec = make_decoder(updates)
    page = [(0x4D, 0x97), (0x74, 0x6C), (0x65, 0x79), (0x20, 0x43)]  # Mö tl ey ' C'
    for seg, (a, c) in enumerate(page):
        dec._decode_group(group0_bytes(seg, a, c), ["A", "B", "C", "D"])
    assert ps_values(updates) == ["Mötley C"]


def group2a_bytes(seg: int, four_bytes: list, flag: int = 0):
    b = (2 << 12) | (flag << 4) | seg
    c = (four_bytes[0] << 8) | four_bytes[1]
    d = (four_bytes[2] << 8) | four_bytes[3]
    return [PI, b, c, d]


def test_rt_extended_chars_need_double_reception():
    """A legit accent and a bit-flipped high bit look identical, so
    extended-char RT segments require the same content twice."""
    updates = []
    dec = make_decoder(updates)
    padded = "M?tley Crue - Looks That Kill".replace("?", "x").ljust(64)
    accent_seg = [0x4D, 0x97, 0x74, 0x6C]   # 'Mötl'

    def feed_pass():
        for seg in range(16):
            if seg == 0:
                dec._decode_group(group2a_bytes(0, accent_seg), ["A", "B", "C", "D"])
            else:
                chunk = [ord(ch) for ch in padded[seg * 4:seg * 4 + 4]]
                dec._decode_group(group2a_bytes(seg, chunk), ["A", "B", "C", "D"])

    feed_pass()
    assert rt_values(updates) == []           # accent segment still pending
    feed_pass()                               # same content again → accepted
    assert rt_values(updates)[-1].startswith("Mötl")


def test_rt_flag_flip_debounced():
    """A single corrupted A/B flag bit must not wipe accumulated segments —
    spurious resets dominated RT latency on weak signals (observed on WMSE:
    RT sometimes took minutes, sometimes seconds)."""
    updates = []
    dec = make_decoder(updates)
    padded = "Nobody owns us but you!".ljust(64)

    # 15 of 16 segments received…
    for seg in range(15):
        chunk = [ord(ch) for ch in padded[seg * 4:seg * 4 + 4]]
        dec._decode_group(group2a_bytes(seg, chunk), ["A", "B", "C", "D"])

    # …then one group arrives with a corrupted (flipped) flag bit
    dec._decode_group(group2a_bytes(7, [0x41, 0x41, 0x41, 0x41], flag=1),
                      ["A", "B", "C", "D"])

    # The final segment with the original flag completes the message —
    # the buffer must have survived the corrupt flag
    chunk = [ord(ch) for ch in padded[60:64]]
    dec._decode_group(group2a_bytes(15, chunk), ["A", "B", "C", "D"])
    assert rt_values(updates)[-1] == "Nobody owns us but you!"

    # A genuine message change (two consecutive new-flag groups) still resets
    new = "Different message".ljust(64)
    for seg in range(16):
        chunk = [ord(ch) for ch in new[seg * 4:seg * 4 + 4]]
        dec._decode_group(group2a_bytes(seg, chunk, flag=1), ["A", "B", "C", "D"])
    assert rt_values(updates)[-1] == "Different message"
