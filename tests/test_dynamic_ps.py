"""Dynamic-PS reassembly tests.

The page sequences here mirror a real station observed over the air
(88.9 FM: "Daughter" / " of Empi" / "re - Hum" / "bird    " →
"Daughter of Empire - Humbird"), including the bit-flipped and truncated
pages RDS actually produces.
"""

from backend.dynamic_ps import DynamicPsAssembler
from backend.metadata import MetadataState

PAGES = ["Daughter", " of Empi", "re - Hum", "bird    "]


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, secs=2.0):
        self.t += secs


def make():
    clock = FakeClock()
    return DynamicPsAssembler(clock=clock), clock


def feed_pages(asm, clock, pages, repeats=1):
    """Feed page cycles at 2 s per page; return all emitted texts."""
    texts = []
    for _ in range(repeats):
        for p in pages:
            res = asm.feed(p)
            if res.text:
                texts.append(res.text)
            clock.tick(2.0)
    return texts


# ---------------------------------------------------------------------------

def test_static_ps_stays_static():
    asm, clock = make()
    for _ in range(20):
        res = asm.feed("WXYZ    ")
        assert res.dynamic is False
        clock.tick(2.0)


def test_paged_ps_reassembles_after_two_clean_cycles():
    asm, clock = make()
    texts = feed_pages(asm, clock, PAGES, repeats=4)
    assert texts == ["Daughter of Empire - Humbird"]  # emitted exactly once


def test_word_boundary_spaces_survive():
    # "Back In " must keep its trailing space or the result reads "Back InBlack"
    pages = ["AC/DC - ", "Back In ", "Black   "]
    asm, clock = make()
    texts = feed_pages(asm, clock, pages, repeats=4)
    assert texts == ["AC/DC - Back In Black"]


def test_corrupted_page_provisionally_shown_then_corrected():
    asm, clock = make()
    corrupted = ["Daughter", " of Empi", "re - Hpi", "bird    "]  # observed bit flip
    feed_pages(asm, clock, corrupted, repeats=1)
    texts = feed_pages(asm, clock, PAGES, repeats=3)
    # The first completed cycle (containing the corrupt page) is emitted
    # provisionally for responsiveness; voting then corrects it.
    assert texts[-1] == "Daughter of Empire - Humbird"
    assert len(texts) <= 2


def test_first_emission_within_one_cycle():
    """Provisional emit: text appears after ~1 cycle, not after full voting."""
    asm, clock = make()
    texts = []
    pages_fed = 0
    for _ in range(3):
        for p in PAGES:
            res = asm.feed(p)
            pages_fed += 1
            if res.text:
                texts.append((pages_fed, res.text))
            clock.tick(2.0)
    assert texts, "nothing emitted"
    first_at, first_text = texts[0]
    assert first_text == "Daughter of Empire - Humbird"
    assert first_at <= len(PAGES) * 2 + 1   # within roughly one cycle of detection


def test_song_change_emits_new_text():
    asm, clock = make()
    texts = feed_pages(asm, clock, PAGES, repeats=3)
    new_pages = ["Halocene", " - Alexa", "ndra Sav", "ior     "]
    texts += feed_pages(asm, clock, new_pages, repeats=6)
    assert texts[0] == "Daughter of Empire - Humbird"
    assert "Halocene - Alexandra Savior" in texts[1:]


def test_reverts_to_static_when_paging_stops():
    asm, clock = make()
    feed_pages(asm, clock, PAGES, repeats=2)
    assert asm.feed("WXYZ    ").dynamic is True   # still dynamic at first
    clock.tick(20.0)                              # > STATIC_SECS with no change
    res = asm.feed("WXYZ    ")
    assert res.dynamic is False


# ---------------------------------------------------------------------------
# MetadataState integration
# ---------------------------------------------------------------------------

async def test_metadata_clears_fragment_name_and_fills_track():
    state = MetadataState()
    clock = FakeClock()
    state._ps_asm = DynamicPsAssembler(clock=clock)
    state.update_tune(88.9e6, "fm")

    # First page is optimistically shown as a station name…
    state.update_rds(ps="Daughter")
    assert state.station_name == "Daughter"

    for _ in range(4):
        for p in PAGES:
            state.update_rds(ps=p)
            clock.tick(2.0)

    # …then paging is detected: fragment cleared, song text parsed
    assert state.station_name is None
    assert state.artist == "Daughter of Empire"
    assert state.title == "Humbird"

    state.update_tune(91.1e6, "fm")   # cancels the debounced history save


async def test_radiotext_wins_over_dynamic_ps():
    state = MetadataState()
    clock = FakeClock()
    state._ps_asm = DynamicPsAssembler(clock=clock)
    state.update_tune(88.9e6, "fm")

    state.update_rds(rt="Real Artist - Real Title")
    for _ in range(4):
        for p in PAGES:
            state.update_rds(ps=p)
            clock.tick(2.0)

    # Dynamic PS must not overwrite RadioText-derived track info
    assert (state.artist, state.title) == ("Real Artist", "Real Title")
    state.update_tune(91.1e6, "fm")
