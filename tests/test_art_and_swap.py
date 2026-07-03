"""Artist/title order auto-swap and art-source precedence tests.

fetch_itunes_art is monkeypatched — no network. The swap scenario mirrors a
real station observed over the air that transmits "Title - Artist"
("Love You Anyway - Devon Gilfillian" → artist field got the song title).
"""

import pytest

import backend.art_lookup as art_lookup
from backend.metadata import MetadataState


@pytest.fixture
def art_file(tmp_path):
    p = tmp_path / "cover.jpg"
    p.write_bytes(b"\xff\xd8fake-jpeg")
    return str(p)


def hit(art_file, artist_name, track_name):
    return {"art_path": art_file, "apple_music_url": "https://music.apple.com/x",
            "artist_name": artist_name, "track_name": track_name}


def fake_fetch(result, calls=None):
    async def _fetch(artist, title):
        if calls is not None:
            calls.append((artist, title))
        return result
    return _fetch


def make_state(artist, title):
    state = MetadataState()
    state.update_tune(88.9e6, "fm")
    state.artist, state.title = artist, title
    return state


# ---------------------------------------------------------------------------
# Order auto-swap
# ---------------------------------------------------------------------------

async def test_reversed_order_swapped(monkeypatch, art_file):
    state = make_state("Love You Anyway", "Devon Gilfillian")   # swapped by station
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(art_file, "Devon Gilfillian", "Love You Anyway")))
    await state._finalize_track()
    assert state.artist == "Devon Gilfillian"
    assert state.title == "Love You Anyway"
    assert state.has_art is True


async def test_correct_order_untouched(monkeypatch, art_file):
    state = make_state("Devon Gilfillian", "Love You Anyway")
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(art_file, "Devon Gilfillian", "Love You Anyway")))
    await state._finalize_track()
    assert state.artist == "Devon Gilfillian"
    assert state.title == "Love You Anyway"


async def test_unrelated_hit_does_not_swap(monkeypatch, art_file):
    # Top hit is a different song entirely — neither straight nor crossed match
    state = make_state("Boards of Canada", "Roygbiv")
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(art_file, "Aphex Twin", "Avril 14th")))
    await state._finalize_track()
    assert state.artist == "Boards of Canada"


async def test_self_titled_ambiguity_not_swapped(monkeypatch, art_file):
    # Straight and crossed both match (e.g. "Iron Maiden - Iron Maiden")
    state = make_state("Iron Maiden", "Iron Maiden")
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(art_file, "Iron Maiden", "Iron Maiden")))
    await state._finalize_track()
    assert (state.artist, state.title) == ("Iron Maiden", "Iron Maiden")


async def test_history_saved_with_corrected_order(monkeypatch, art_file, tmp_db):
    await tmp_db.init_db()
    state = make_state("Love You Anyway", "Devon Gilfillian")
    state.station_name = "WXYZ"
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(art_file, "Devon Gilfillian", "Love You Anyway")))
    await state._finalize_track()
    db = await tmp_db.get_db()
    async with db.execute("SELECT artist, title FROM history") as cur:
        rows = await cur.fetchall()
    assert [(r["artist"], r["title"]) for r in rows] == [
        ("Devon Gilfillian", "Love You Anyway")
    ]


# ---------------------------------------------------------------------------
# Art-source precedence: LOT (HD) always beats iTunes
# ---------------------------------------------------------------------------

async def test_lot_art_blocks_itunes_lookup(monkeypatch, art_file):
    state = make_state("Artist", "Title")
    state.update_nrsc5(art_path=art_file)          # HD LOT art arrives
    assert state._art_source == "lot"

    calls = []
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(art_file, "Artist", "Title"), calls))
    await state._finalize_track()
    assert calls == []                             # lookup never even attempted


async def test_lot_art_wins_race_during_lookup(monkeypatch, art_file, tmp_path):
    """LOT art lands while the iTunes request is in flight — iTunes result
    must be discarded."""
    state = make_state("Artist", "Title")
    lot_file = tmp_path / "lot.jpg"
    lot_file.write_bytes(b"\xff\xd8lot")

    async def slow_fetch(artist, title):
        state.update_nrsc5(art_path=str(lot_file))   # LOT arrives mid-lookup
        return hit(art_file, "Artist", "Title")

    monkeypatch.setattr(art_lookup, "fetch_itunes_art", slow_fetch)
    await state._finalize_track()
    assert state._art_source == "lot"
    assert state._itunes_art_applied is None       # iTunes art never written


async def test_lot_art_overwrites_earlier_itunes_art(monkeypatch, art_file, tmp_path):
    state = make_state("Artist", "Title")
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(art_file, "Artist", "Title")))
    await state._finalize_track()
    assert state._art_source == "itunes"
    v = state.art_version

    lot_file = tmp_path / "lot.jpg"
    lot_file.write_bytes(b"\xff\xd8lot")
    state.update_nrsc5(art_path=str(lot_file))
    assert state._art_source == "lot"
    assert state.art_version == v + 1              # LOT replaced the iTunes art


async def test_itunes_art_refreshes_on_song_change(monkeypatch, tmp_path):
    """iTunes art from the previous song must not stick to the next one."""
    state = make_state("Artist One", "Song One")
    art1 = tmp_path / "one.jpg"
    art2 = tmp_path / "two.jpg"
    art1.write_bytes(b"\xff\xd8one")
    art2.write_bytes(b"\xff\xd8two")

    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(str(art1), "Artist One", "Song One")))
    await state._finalize_track()
    v1 = state.art_version

    state.artist, state.title = "Artist Two", "Song Two"
    monkeypatch.setattr(art_lookup, "fetch_itunes_art",
                        fake_fetch(hit(str(art2), "Artist Two", "Song Two")))
    await state._finalize_track()
    assert state.art_version == v1 + 1             # new art applied
