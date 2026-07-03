import asyncio

from backend.metadata import MetadataState, _parse_rt, _rds_similar


# ---------------------------------------------------------------------------
# RadioText parsing
# ---------------------------------------------------------------------------

class TestParseRt:
    def test_dash_separator(self):
        assert _parse_rt("Boards of Canada - Roygbiv") == ("Boards of Canada", "Roygbiv")

    def test_dash_splits_on_first_occurrence(self):
        # A dash inside the title must stay with the title
        assert _parse_rt("Artist - Title - Subtitle") == ("Artist", "Title - Subtitle")

    def test_by_separator(self):
        assert _parse_rt("Roygbiv by Boards of Canada") == ("Boards of Canada", "Roygbiv")

    def test_by_inside_title_splits_on_last_by(self):
        assert _parse_rt("Stand By Me by Ben E. King") == ("Ben E. King", "Stand By Me")

    def test_short_artist_after_by_is_rejected(self):
        # "Me" is too short to be an artist; the whole string is the title
        assert _parse_rt("Stand By Me") == (None, "Stand By Me")

    def test_no_separator(self):
        assert _parse_rt("WXYZ Traffic and Weather") == (None, "WXYZ Traffic and Weather")


# ---------------------------------------------------------------------------
# Fuzzy RDS comparison
# ---------------------------------------------------------------------------

class TestRdsSimilar:
    def test_identical(self):
        assert _rds_similar("The Devil Is in the Details", "The Devil Is in the Details")

    def test_bit_corruption_tolerated(self):
        assert _rds_similar("The Devil Is iinthe Details", "The Devil Is in the Details")
        assert _rds_similar("Boards of Canada ⬛G", "Boards of Canada")

    def test_different_strings_rejected(self):
        assert not _rds_similar("Boards of Canada", "Aphex Twin")

    def test_case_insensitive(self):
        assert _rds_similar("ROYGBIV", "Roygbiv")

    def test_none_handling(self):
        assert _rds_similar(None, None)
        assert not _rds_similar("x", None)
        assert not _rds_similar(None, "x")


# ---------------------------------------------------------------------------
# RT+ priority over heuristic RadioText parsing
# ---------------------------------------------------------------------------

async def test_rtplus_suppresses_heuristic_rt():
    state = MetadataState()
    state.update_tune(91.1e6, "fm")

    # Heuristic parsing fills artist/title from plain RT
    state.update_rds(rt="Wrong Artist - Wrong Title")
    assert state.artist == "Wrong Artist"

    # RT+ structured tags take over…
    state.update_rds(rtp_artist="Real Artist", rtp_title="Real Title")
    assert (state.artist, state.title) == ("Real Artist", "Real Title")

    # …and later plain RT must no longer clobber them
    state.update_rds(rt="Stale Text - From Buffer")
    assert (state.artist, state.title) == ("Real Artist", "Real Title")

    # Cancel the debounced history-save task started by update_rds
    state.update_tune(88.5e6, "fm")
    await asyncio.sleep(0)


async def test_stale_generation_callbacks_dropped():
    """A retune must invalidate queued RDS/nrsc5 callbacks from the previous
    station's pipeline (observed live: previous station's track surviving
    onto the new station)."""
    state = MetadataState()
    state.update_tune(91.7e6, "fm")
    old_gen = state.tune_generation
    state.update_rds(rt="Lizzy Borden - American Metal", gen=old_gen)
    assert state.artist == "Lizzy Borden"

    state.update_tune(106.9e6, "fm")   # user retunes; old pipeline winding down

    # Late callback from the old pipeline arrives after the clear
    state.update_rds(rt="Lizzy Borden - American Metal", gen=old_gen)
    assert state.artist is None
    state.update_nrsc5(artist="Stale", title="Stale", gen=old_gen)
    assert state.artist is None

    # Current-generation data still lands
    state.update_rds(rt="New Artist - New Title", gen=state.tune_generation)
    assert state.artist == "New Artist"
    state.update_tune(88.5e6, "fm")


async def test_update_tune_resets_metadata():
    state = MetadataState()
    state.update_rds(ps="WXYZ", rt="A - B", pty="Rock")
    state.update_tune(88.5e6, "fm")
    assert state.station_name is None
    assert state.artist is None
    assert state.title is None
    assert state.state == "tuning"
    await asyncio.sleep(0)
