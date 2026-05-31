import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import init_db
from .metadata import state as meta
from .radio.manager import RadioManager
from .recorder import Recorder
from .stations import (
    PresetCreate,
    PresetUpdate,
    create_preset,
    delete_preset,
    get_preset,
    list_presets,
    seed_default_presets,
    update_preset,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
_EXAMPLE_PATH = _CONFIG_PATH + ".example"


def _load_config() -> dict:
    path = _CONFIG_PATH if os.path.exists(_CONFIG_PATH) else _EXAMPLE_PATH
    with open(path) as f:
        return yaml.safe_load(f)


config = _load_config()

_HLS_DIR = config.get("hls", {}).get("segment_dir", "/tmp/sdr-hls")
_ART_DIR = "/tmp/sdr-art"

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

radio: Optional[RadioManager] = None
recorder: Optional[Recorder] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global radio, recorder
    await init_db()

    os.makedirs(_HLS_DIR, exist_ok=True)
    os.makedirs(_ART_DIR, exist_ok=True)

    radio = RadioManager(config, meta)
    recorder = Recorder(config, meta)

    await radio.startup()
    await recorder.startup()

    # Seed default presets from config if DB is empty
    defaults = config.get("default_presets", [])
    if defaults:
        await seed_default_presets(defaults)

    yield

    await radio.stop()
    await recorder.shutdown()


app = FastAPI(title="Squelch", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

app.mount("/hls", StaticFiles(directory=_HLS_DIR), name="hls")
app.mount("/art", StaticFiles(directory=_ART_DIR), name="art")

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


# ---------------------------------------------------------------------------
# Tune
# ---------------------------------------------------------------------------


class TuneRequest(BaseModel):
    frequency: float          # MHz for FM/HD/scanner, kHz for AM
    band: str                 # fm | am | scanner | hd
    gain: Optional[str] = None
    bandwidth: Optional[str] = None   # wide | narrow
    stereo_mode: Optional[str] = None  # auto | stereo | mono


@app.post("/tune")
async def tune(req: TuneRequest):
    band = req.band.lower()
    if band not in ("fm", "am", "scanner", "hd"):
        raise HTTPException(400, "band must be fm, am, scanner, or hd")

    # Normalize frequency to Hz
    if band in ("fm", "hd", "scanner"):
        freq_hz = req.frequency * 1e6
    else:
        # AM: user enters kHz
        freq_hz = req.frequency * 1e3

    kwargs = {}
    if req.gain is not None:
        kwargs["gain"] = req.gain
    if req.bandwidth is not None:
        kwargs["bandwidth"] = req.bandwidth
    if req.stereo_mode is not None:
        kwargs["stereo_mode"] = req.stereo_mode

    asyncio.create_task(radio.tune(freq_hz, band, **kwargs))
    return {"status": "tuning", "frequency": req.frequency, "band": band}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@app.get("/status")
async def status():
    return {**meta.to_dict(), **radio.status()}


@app.get("/stream/url")
async def stream_url():
    return {"hls_url": f"/hls/stream.m3u8"}


# ---------------------------------------------------------------------------
# WebSocket — live metadata + signal
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    meta.register_ws(ws)
    try:
        # Send current state immediately on connect
        await ws.send_text(json.dumps(meta.to_dict()))
        # Keep alive — client messages are ignored, server pushes updates
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        meta.unregister_ws(ws)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@app.get("/presets")
async def get_presets():
    return await list_presets()


@app.post("/presets", status_code=201)
async def post_preset(p: PresetCreate):
    return await create_preset(p)


@app.patch("/presets/{preset_id}")
async def patch_preset(preset_id: int, p: PresetUpdate):
    result = await update_preset(preset_id, p)
    if not result:
        raise HTTPException(404, "Preset not found")
    return result


@app.delete("/presets/{preset_id}", status_code=204)
async def remove_preset(preset_id: int):
    if not await delete_preset(preset_id):
        raise HTTPException(404, "Preset not found")


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------


class RecordStartRequest(BaseModel):
    filename: Optional[str] = None


@app.post("/record/start")
async def record_start(req: RecordStartRequest = RecordStartRequest()):
    return await recorder.start(req.filename)


@app.post("/record/stop")
async def record_stop():
    return await recorder.stop()


@app.get("/record/status")
async def record_status():
    return {
        "recording": recorder.is_recording(),
        "file": recorder._recording_file and os.path.basename(recorder._recording_file),
    }


@app.get("/recordings")
async def get_recordings():
    return await recorder.list_recordings()


@app.delete("/recordings/{recording_id}", status_code=204)
async def delete_rec(recording_id: int):
    if not await recorder.delete_recording(recording_id):
        raise HTTPException(404, "Recording not found")


@app.get("/recordings/{recording_id}/download")
async def download_recording(recording_id: int):
    recs = await recorder.list_recordings()
    rec = next((r for r in recs if r["id"] == recording_id), None)
    if not rec:
        raise HTTPException(404, "Recording not found")
    output_dir = os.path.expanduser(
        config.get("recordings", {}).get("output_dir", "~/recordings")
    )
    path = os.path.join(output_dir, rec["filename"])
    if not os.path.exists(path):
        raise HTTPException(404, "File not found on disk")
    return FileResponse(path, media_type="audio/mp4", filename=rec["filename"])


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@app.get("/history")
async def get_history():
    from .db import get_db

    db = await get_db()
    try:
        async with db.execute(
            "SELECT * FROM history ORDER BY seen_at DESC LIMIT 100"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    srv_cfg = config.get("server", {})
    uvicorn.run(
        "backend.main:app",
        host=srv_cfg.get("host", "0.0.0.0"),
        port=srv_cfg.get("port", 8000),
        reload=False,
    )
