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
