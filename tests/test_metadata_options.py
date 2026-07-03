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
        state._track_confident = True   # simulates confident data
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
# Confidence gate: display fast, write history once confident
# ---------------------------------------------------------------------------

async def test_partial_rt_displays_but_never_saves(tmp_db, monkeypatch):
    await tmp_db.init_db()
    state = make_state()
    state.station_name = "WMSE"
    calls = []

    async def spy_fetch(artist, title):
        calls.append((artist, title))
        return None
    monkeypatch.setattr(art_lookup, "fetch_itunes_art", spy_fetch)

    # Partial RadioText: shown immediately…
    state.update_rds(rt="Little Richard - Taxi Blu", rt_partial=True)
    assert state.title == "Taxi Blu"
    await state._finalize_track()
    assert calls == []                       # no lookup from partial data
    db = await tmp_db.get_db()
    async with db.execute("SELECT COUNT(*) FROM history") as cur:
        assert (await cur.fetchone())[0] == 0    # no history row either

    # …then the complete version arrives: lookup + history unlock
    state.station_name = "WMSE"              # cleared? keep set
    state.update_rds(rt="Little Richard - Taxi Blues", rt_partial=False)
    await state._finalize_track()
    assert calls == [("Little Richard", "Taxi Blues")]
    async with db.execute("SELECT artist, title FROM history") as cur:
        rows = await cur.fetchall()
    assert [(r["artist"], r["title"]) for r in rows] == [("Little Richard", "Taxi Blues")]
    state.update_tune(91.1e6, "fm")


async def test_provisional_ps_not_saved_until_confident(tmp_db, monkeypatch):
    await tmp_db.init_db()
    state = make_state()
    state.station_name = "WXYZ"
    monkeypatch.setattr(art_lookup, "fetch_itunes_art", fake_fetch(None, []))
    clock = state._ps_asm._clock

    saved = []
    orig_save = state.save_history

    async def spy_save():
        saved.append(state._track_confident)
        await orig_save()
    state.save_history = spy_save

    for i, cycles in enumerate(range(4)):
        for p in SONG:
            state.update_rds(ps=p)
            clock.tick(2.0)
        state.station_name = "WXYZ"   # dynamic clears it; re-pin for save
        await state._finalize_track()

    # Every history write happened with confident data only
    assert saved, "confident version never saved"
    assert all(saved)
    db = await tmp_db.get_db()
    async with db.execute("SELECT artist FROM history") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1                    # written once, not write-then-fix
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
