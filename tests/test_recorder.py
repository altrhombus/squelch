import asyncio
import os

import pytest

from backend.metadata import MetadataState
from backend.recorder import Recorder, _auto_filename, _safe_name


class FakeStreams:
    def new_client(self):
        return asyncio.Queue()

    def remove_client(self, q):
        pass


class FakeRadio:
    """Records tune() calls; mimics RadioManager.status()."""

    def __init__(self, freq_hz=94.7e6, band="fm"):
        self.calls = []
        self._freq, self._band = freq_hz, band

    def status(self):
        return {"running": True, "frequency": self._freq, "band": self._band}

    async def tune(self, freq_hz, band, **kwargs):
        self.calls.append((freq_hz, band))
        self._freq, self._band = freq_hz, band


@pytest.fixture
async def recorder(tmp_db, tmp_path):
    rec = Recorder({"recordings": {"output_dir": str(tmp_path)}}, MetadataState())
    rec.set_streaming(FakeStreams())
    return rec


# ---------------------------------------------------------------------------
# Filename handling
# ---------------------------------------------------------------------------

async def test_traversal_filename_is_pinned_to_output_dir(recorder, tmp_db, tmp_path):
    await tmp_db.init_db()
    result = await recorder.start("../../../../etc/evil.aac")
    assert result["file"] == "evil.aac"
    assert (tmp_path / "evil.aac").exists()
    stopped = await recorder.stop()
    assert stopped["status"] == "stopped"


async def test_dotfile_and_empty_filenames_rejected(recorder):
    for bad in ("...", ".hidden.aac", "   "):
        assert (await recorder.start(bad)).get("error") == "invalid filename"


def test_safe_name_strips_specials():
    assert _safe_name("AC/DC: Back in Black!") == "AC_DC__Back_in_Black_"


def test_auto_filename_includes_station_and_track(tmp_path):
    meta = MetadataState()
    meta.station_name = "WXYZ"
    meta.artist, meta.title = "Boards of Canada", "Roygbiv"
    name = os.path.basename(_auto_filename(meta, str(tmp_path)))
    assert "WXYZ" in name
    assert "Boards_of_Canada-Roygbiv" in name
    assert name.endswith(".aac")


# ---------------------------------------------------------------------------
# Stop robustness
# ---------------------------------------------------------------------------

async def test_stop_when_not_recording(recorder):
    assert (await recorder.stop()).get("error") == "not recording"


async def test_stop_cleans_up_after_write_task_died(recorder, tmp_db, tmp_path):
    await tmp_db.init_db()
    await recorder.start("x.aac")
    # Simulate the write task dying on its own (e.g. unexpected exception)
    recorder._rec_task.cancel()
    try:
        await recorder._rec_task
    except asyncio.CancelledError:
        pass
    recorder._rec_task = None

    result = await recorder.stop()
    assert result["status"] == "stopped"
    assert recorder._rec_file is None  # file handle actually closed


# ---------------------------------------------------------------------------
# Scheduled recordings
# ---------------------------------------------------------------------------

async def test_scheduled_run_tunes_records_and_restores(recorder, tmp_db, tmp_path):
    await tmp_db.init_db()
    radio = FakeRadio(freq_hz=94.7e6, band="fm")
    recorder.set_radio(radio)

    await recorder._run_scheduled(
        {"name": "Morning Show", "frequency": 91.1, "band": "fm", "duration_seconds": 0}
    )

    assert radio.calls[0] == (91.1e6, "fm")   # tuned to the scheduled station
    assert radio.calls[1] == (94.7e6, "fm")   # restored the previous one
    files = [f for f in os.listdir(tmp_path) if "Morning_Show" in f]
    assert files, "recording file was not created"


async def test_scheduled_run_uses_khz_for_am(recorder, tmp_db):
    await tmp_db.init_db()
    radio = FakeRadio()
    recorder.set_radio(radio)
    await recorder._run_scheduled(
        {"name": "AM Test", "frequency": 1000, "band": "am", "duration_seconds": 0}
    )
    assert radio.calls[0] == (1000e3, "am")


async def test_scheduled_run_skipped_while_manual_recording(recorder, tmp_db):
    await tmp_db.init_db()
    radio = FakeRadio()
    recorder.set_radio(radio)
    await recorder.start("manual.aac")
    await recorder._run_scheduled(
        {"name": "Clash", "frequency": 91.1, "band": "fm", "duration_seconds": 0}
    )
    assert radio.calls == []          # never touched the tuner
    assert recorder.is_recording()    # manual recording untouched
    await recorder.stop()


async def test_schedule_crud_round_trip(recorder, tmp_db):
    await tmp_db.init_db()
    await recorder.startup()
    try:
        sched = await recorder.create_scheduled_recording({
            "name": "Nightly", "frequency": 88.5, "band": "fm",
            "duration_seconds": 120, "cron_expr": "0 2 * * *",
        })
        assert recorder._scheduler.get_job(f"sched_{sched['id']}") is not None
        assert any(s["id"] == sched["id"] for s in await recorder.list_scheduled_recordings())

        assert await recorder.delete_scheduled_recording(sched["id"]) is True
        assert recorder._scheduler.get_job(f"sched_{sched['id']}") is None
        assert await recorder.delete_scheduled_recording(sched["id"]) is False
    finally:
        await recorder.shutdown()
