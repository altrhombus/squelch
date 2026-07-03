"""Config toggles: itunes_lookup, order_correction, show_ps_messages."""

import pytest

import backend.art_lookup as art_lookup
from backend.dynamic_ps import DynamicPsAssembler
from backend.metadata import MetadataState


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, secs=2.0):
        self.t += secs


def make_state(cfg=None, artist=None, title=None):
    state = MetadataState({"metadata": cfg} if cfg is not None else None)
    state._ps_asm = DynamicPsAssembler(clock=FakeClock())
    state.update_tune(88.9e6, "fm")
    if artist or title:
        state.artist, state.title = artist, title
    return state


def feed_message(state, pages, repeats=4):
    clock = state._ps_asm._clock
    for _ in range(repeats):
        for p in pages:
            state.update_rds(ps=p)
            clock.tick(2.0)


PROMO = ["Mindy No", "votny We", "ekdays 3", "pm      "]     # 4-page promo
SONG  = ["Daughter", " of Empi", "re - Hum", "bird    "]


# ---------------------------------------------------------------------------
# show_ps_messages
# ---------------------------------------------------------------------------

async def test_promo_shown_on_title_line_by_default():
    state = make_state()
    feed_message(state, PROMO)
    assert state.artist is None
    assert state.title == "Mindy Novotny Weekdays 3pm"   # padding collapsed
    state.update_tune(91.1e6, "fm")


async def test_promo_hidden_when_disabled():
    state = make_state(cfg={"show_ps_messages": False})
    feed_message(state, PROMO)
    assert state.title is None
    state.update_tune(91.1e6, "fm")


async def test_song_text_still_beats_promo_handling():
    state = make_state()
    feed_message(state, SONG)
    assert (state.artist, state.title) == ("Daughter of Empire", "Humbird")
    state.update_tune(91.1e6, "fm")


async def test_slow_pager_keeps_name_and_gains_track():
    """Slow pagers show PS fragments as the station name (car-radio
    behavior) but the track must still populate once assembled."""
    state = make_state()
    clock = state._ps_asm._clock
    for _ in range(3):
        for p in SONG:
            state.update_rds(ps=p)
            clock.tick(40.0)
    assert (state.artist, state.title) == ("Daughter of Empire", "Humbird")
    assert state.station_name == "bird"   # last fragment still shown as name
    state.update_tune(91.1e6, "fm")


async def test_multiseparator_provisional_never_shown():
    """A provisional loop with a corrupt page spliced in produces text with
    two ' - ' separators ('ing - Stthe feelthg - St' observed live) — it
    must wait for the confident pass; only the clean text ever displays."""
    state = make_state()
    clock = state._ps_asm._clock
    corrupt_pass = ["ing - St", "the feel", "thg - St"]
    clean = ["the feel", "ing - St", "eve Lacy"]
    observed = set()
    for p in corrupt_pass:
        state.update_rds(ps=p)
        observed.add((state.artist, state.title))
        clock.tick(2.0)
    for _ in range(4):
        for p in clean:
            state.update_rds(ps=p)
            observed.add((state.artist, state.title))
            clock.tick(2.0)
    assert all(a != "ing" for a, _ in observed if a)
    assert (state.artist, state.title) == ("the feeling", "Steve Lacy")
    state.update_tune(91.1e6, "fm")


async def test_short_fragments_never_shown_even_with_messages_on():
    """Garbled fragments are filtered regardless of the setting — the message
    path requires >=3 pages of evidence."""
    state = make_state()
    feed_message(state, [" Tomatoe", "s - Lucy"], repeats=6)
    assert state.artist is None
    assert state.title is None
    state.update_tune(91.1e6, "fm")


# ---------------------------------------------------------------------------
# itunes_lookup / order_correction
# ---------------------------------------------------------------------------

@pytest.fixture
def art_file(tmp_path):
    p = tmp_path / "cover.jpg"
    p.write_bytes(b"\xff\xd8fake")
    return str(p)


def fake_fetch(result, calls):
    async def _fetch(artist, title):
        calls.append((artist, title))
        return result
    return _fetch


async def test_itunes_lookup_disabled_skips_network(monkeypatch, art_file):
    state = make_state(cfg={"itunes_lookup": False},
                       artist="Love You Anyway", title="Devon Gilfillian")
    calls = []
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch({"art_path": art_file,
                                    "artist_name": "Devon Gilfillian",
                                    "track_name": "Love You Anyway"}, calls))
    await state._finalize_track()
    assert calls == []                    # never contacted
    assert state.has_art is False
    assert state.artist == "Love You Anyway"   # no swap either


async def test_order_correction_disabled_keeps_fields(monkeypatch, art_file):
    state = make_state(cfg={"order_correction": False},
                       artist="Love You Anyway", title="Devon Gilfillian")
    calls = []
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch({"art_path": art_file,
                                    "apple_music_url": None,
                                    "artist_name": "Devon Gilfillian",
                                    "track_name": "Love You Anyway"}, calls))
    await state._finalize_track()
    assert calls, "lookup should still run for art"
    assert state.has_art is True                 # art still applied
    assert state.artist == "Love You Anyway"     # but no swap
