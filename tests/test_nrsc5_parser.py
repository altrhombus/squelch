from backend.radio.nrsc5_backend import Nrsc5Backend


def make_backend(updates: list):
    return Nrsc5Backend(metadata_callback=updates.append)


def test_station_and_track_metadata():
    updates = []
    b = make_backend(updates)
    b._handle_line("12:00:00 Station name: WXYZ-HD")
    b._handle_line("12:00:01 Title: Roygbiv")
    b._handle_line("12:00:01 Artist: Boards of Canada")
    b._handle_line("12:00:02 Slogan: The Best Mix")
    b._handle_line("12:00:02 Program type: Rock")
    merged = {k: v for u in updates for k, v in u.items()}
    assert merged["station_name"] == "WXYZ-HD"
    assert merged["title"] == "Roygbiv"
    assert merged["artist"] == "Boards of Canada"
    assert merged["slogan"] == "The Best Mix"
    assert merged["pty"] == "Rock"


def test_sync_lock_and_loss():
    updates = []
    b = make_backend(updates)
    b._handle_line("12:00:00 Synchronized")
    assert updates[-1]["hd_locked"] is True
    b._handle_line("12:05:00 Lost synchronization")
    assert updates[-1]["hd_locked"] is False


def test_available_programs_accumulate_as_1_based():
    updates = []
    b = make_backend(updates)
    b._handle_line("12:00:00 Audio program 0: ...")
    assert updates[-1]["hd_channels_available"] == [1]
    b._handle_line("12:00:01 Audio program 2: ...")
    assert updates[-1]["hd_channels_available"] == [1, 3]


def test_lot_art_path_resolved_and_existence_checked(tmp_path):
    updates = []
    b = make_backend(updates)
    b._art_dir = str(tmp_path)

    # Missing file → no art_path in the update
    b._handle_line("12:00:00 LOT file: cover.jpg")
    assert not any("art_path" in u for u in updates)

    # Bare filename resolves against the art dir once the file exists
    art = tmp_path / "cover.jpg"
    art.write_bytes(b"\xff\xd8fake")
    b._handle_line("12:00:01 LOT file: cover.jpg")
    assert updates[-1]["art_path"] == str(art)


def test_irrelevant_lines_produce_no_update():
    updates = []
    b = make_backend(updates)
    b._handle_line("12:00:00 MER: 12.3 dB")
    assert updates == []
