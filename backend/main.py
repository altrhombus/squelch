import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import init_db
from .icecast import IcecastPusher
from .metadata import state as meta
from .radio.manager import RadioManager
from .recorder import Recorder
from .streaming import StreamingManager
from .stations import (
    PresetCreate,
    PresetUpdate,
    create_preset,
    delete_preset,
    list_presets,
    seed_default_presets,
    update_preset,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH  = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
_EXAMPLE_PATH = _CONFIG_PATH + ".example"
_ART_DIR      = "/tmp/sdr-art"


def _load_config() -> dict:
    path = _CONFIG_PATH if os.path.exists(_CONFIG_PATH) else _EXAMPLE_PATH
    with open(path) as f:
        return yaml.safe_load(f)


config = _load_config()

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

radio:    Optional[RadioManager]    = None
recorder: Optional[Recorder]       = None
streams:  Optional[StreamingManager] = None
icecast:  Optional[IcecastPusher]  = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global radio, recorder, streams, icecast
    await init_db()

    os.makedirs(_ART_DIR, exist_ok=True)

    streams  = StreamingManager()
    radio    = RadioManager(config, meta, streams)
    recorder = Recorder(config, meta)
    recorder.set_streaming(streams)
    recorder.set_radio(radio)

    await radio.startup()
    await recorder.startup()

    ice_cfg = config.get("icecast") or {}
    if ice_cfg.get("enabled"):
        icecast = IcecastPusher(ice_cfg, meta, streams)
        icecast.start()

    defaults = config.get("default_presets", [])
    if defaults:
        await seed_default_presets(defaults)

    yield

    if icecast:
        await icecast.stop()
    await radio.stop()
    await recorder.shutdown()


app = FastAPI(title="Squelch", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

os.makedirs(_ART_DIR, exist_ok=True)
app.mount("/art",    StaticFiles(directory=_ART_DIR), name="art")

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


# ---------------------------------------------------------------------------
# Audio stream  (replaces HLS)
# ---------------------------------------------------------------------------

@app.get("/stream")
async def audio_stream(request: Request):
    """
    Chunked HTTP AAC-LC/ADTS stream.
    Content-Type: audio/aac — natively decoded on iOS/macOS, Chrome 89+, AirPlay.
    Each connected client gets its own asyncio.Queue; slow clients drop frames.
    """
    q = streams.new_client()

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=10.0)
                    yield chunk
                except asyncio.TimeoutError:
                    # Keep the connection alive even if SDR is stopped
                    pass
        finally:
            streams.remove_client(q)

    return StreamingResponse(
        generate(),
        media_type="audio/aac",
        headers={
            "Cache-Control":        "no-cache, no-store",
            "X-Accel-Buffering":    "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# Tune
# ---------------------------------------------------------------------------

class TuneRequest(BaseModel):
    frequency:   float
    band:        str
    gain:        Optional[str] = None
    stereo_mode: Optional[str] = None
    hd_channel:  Optional[int] = None   # 1-based HD sub-channel (HD1=1, HD2=2, …)


@app.post("/tune")
async def tune(req: TuneRequest):
    band = req.band.lower()
    if band not in ("fm", "am", "scanner", "hd", "wx"):
        raise HTTPException(400, "band must be fm, am, scanner, hd, or wx")

    freq_hz = req.frequency * 1e6 if band != "am" else req.frequency * 1e3

    kwargs: dict = {}
    if req.gain        is not None: kwargs["gain"]       = req.gain
    if req.stereo_mode is not None: kwargs["stereo_mode"] = req.stereo_mode
    if req.hd_channel  is not None: kwargs["hd_channel"] = req.hd_channel

    _spawn_bg(radio.tune(freq_hz, band, **kwargs))
    return {"status": "tuning", "frequency": req.frequency, "band": band}


# Keep references to fire-and-forget tasks — asyncio only holds weak refs, so
# an unreferenced task can be garbage-collected mid-flight. The done callback
# also surfaces exceptions that would otherwise be silently dropped.
_bg_tasks: set = set()


def _spawn_bg(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_done)
    return task


def _bg_done(task: asyncio.Task):
    _bg_tasks.discard(task)
    if not task.cancelled() and task.exception():
        logger.error("Background task failed", exc_info=task.exception())


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/status")
async def status():
    return {**meta.to_dict(), **radio.status()}


@app.post("/squelch")
async def set_squelch(body: dict):
    slider = int(body.get("slider", 0))
    radio.set_squelch(max(0, min(100, slider)))
    return {"slider": slider}


@app.get("/stream/url")
async def stream_url():
    return {"stream_url": "/stream", "content_type": "audio/aac"}


# ---------------------------------------------------------------------------
# WebSocket — live metadata + signal
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    meta.register_ws(ws)
    try:
        await ws.send_text(json.dumps(meta.to_dict()))
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
    result = {
        "recording": recorder.is_recording(),
        "file": recorder._recording_file and os.path.basename(recorder._recording_file),
    }
    if recorder.is_recording() and recorder._rec_start is not None:
        result["started_at"] = recorder._rec_start.timestamp()
    return result


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
    rec  = next((r for r in recs if r["id"] == recording_id), None)
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
# Scheduled recordings
# ---------------------------------------------------------------------------

class ScheduleCreate(BaseModel):
    name:             str
    frequency:        float   # display units: MHz (kHz for AM), same as presets
    band:             str
    duration_seconds: int
    cron_expr:        str     # standard 5-field crontab expression


@app.get("/schedules")
async def get_schedules():
    return await recorder.list_scheduled_recordings()


@app.post("/schedules", status_code=201)
async def post_schedule(req: ScheduleCreate):
    if req.band.lower() not in ("fm", "am", "scanner", "hd", "wx"):
        raise HTTPException(400, "band must be fm, am, scanner, hd, or wx")
    if req.duration_seconds <= 0:
        raise HTTPException(400, "duration_seconds must be positive")
    from apscheduler.triggers.cron import CronTrigger
    try:
        CronTrigger.from_crontab(req.cron_expr)
    except ValueError as e:
        raise HTTPException(400, f"invalid cron expression: {e}")
    return await recorder.create_scheduled_recording({
        "name":             req.name,
        "frequency":        req.frequency,
        "band":             req.band.lower(),
        "duration_seconds": req.duration_seconds,
        "cron_expr":        req.cron_expr,
    })


@app.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: int):
    if not await recorder.delete_scheduled_recording(schedule_id):
        raise HTTPException(404, "Schedule not found")


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


@app.delete("/history/{history_id}", status_code=204)
async def delete_history_item(history_id: int):
    from .db import get_db
    db = await get_db()
    try:
        await db.execute("DELETE FROM history WHERE id = ?", (history_id,))
        await db.commit()
    finally:
        await db.close()


@app.delete("/history", status_code=204)
async def clear_history():
    from .db import get_db
    db = await get_db()
    try:
        await db.execute("DELETE FROM history")
        await db.commit()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    srv = config.get("server", {})
    uvicorn.run("backend.main:app", host=srv.get("host", "0.0.0.0"),
                port=srv.get("port", 8000), reload=False)
