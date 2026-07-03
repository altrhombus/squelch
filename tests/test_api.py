"""HTTP-level API tests using the full app (lifespan included, no SDR)."""

import pytest
from fastapi.testclient import TestClient

import backend.main as main_mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    import backend.db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", str(tmp_path / "api.db"))

    cfg = {
        "server": {"host": "127.0.0.1", "port": 8000},
        "sdr": {"device_index": 0, "gain": "auto", "ppm_correction": 0, "deemphasis_us": 75},
        "recordings": {"output_dir": str(tmp_path / "recordings")},
        "default_presets": [{"name": "Example FM", "frequency": 91.1, "band": "fm"}],
    }
    monkeypatch.setattr(main_mod, "_load_config", lambda: cfg)

    # Lifespan runs on enter (init_db, singletons) and shutdown on exit (close_db)
    with TestClient(main_mod.app) as c:
        yield c


def test_root_serves_frontend(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_status_reports_idle(client):
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "idle"
    assert body["running"] is False


def test_tune_rejects_unknown_band(client):
    r = client.post("/tune", json={"frequency": 91.1, "band": "shortwave"})
    assert r.status_code == 400


def test_squelch_clamps_slider(client):
    assert client.post("/squelch", json={"slider": 250}).json() == {"slider": 100}
    assert client.post("/squelch", json={"slider": -5}).json() == {"slider": 0}


def test_presets_seeded_and_crud(client):
    presets = client.get("/presets").json()
    assert [p["name"] for p in presets] == ["Example FM"]   # seeded from config

    created = client.post("/presets", json={
        "name": "WBEZ", "frequency": 91.5, "band": "fm",
    })
    assert created.status_code == 201
    pid = created.json()["id"]

    patched = client.patch(f"/presets/{pid}", json={"name": "WBEZ 91.5"})
    assert patched.json()["name"] == "WBEZ 91.5"

    assert client.delete(f"/presets/{pid}").status_code == 204
    assert client.delete(f"/presets/{pid}").status_code == 404


def test_history_empty_and_clear(client):
    assert client.get("/history").json() == []
    assert client.delete("/history").status_code == 204


def test_record_status_idle(client):
    body = client.get("/record/status").json()
    assert body == {"recording": False, "file": None}


def test_schedules_validation_and_crud(client):
    bad_cron = client.post("/schedules", json={
        "name": "x", "frequency": 91.1, "band": "fm",
        "duration_seconds": 60, "cron_expr": "not a cron",
    })
    assert bad_cron.status_code == 400

    bad_duration = client.post("/schedules", json={
        "name": "x", "frequency": 91.1, "band": "fm",
        "duration_seconds": 0, "cron_expr": "0 9 * * *",
    })
    assert bad_duration.status_code == 400

    created = client.post("/schedules", json={
        "name": "Morning Show", "frequency": 91.1, "band": "fm",
        "duration_seconds": 3600, "cron_expr": "0 9 * * 1-5",
    })
    assert created.status_code == 201
    sid = created.json()["id"]

    assert any(s["id"] == sid for s in client.get("/schedules").json())
    assert client.delete(f"/schedules/{sid}").status_code == 204
    assert client.delete(f"/schedules/{sid}").status_code == 404


def test_stream_url(client):
    assert client.get("/stream/url").json() == {
        "stream_url": "/stream", "content_type": "audio/aac",
    }


def test_websocket_sends_initial_metadata(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["state"] == "idle"
        assert "frequency" in msg
