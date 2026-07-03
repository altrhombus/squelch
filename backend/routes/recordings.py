"""Manual recordings and cron-scheduled recordings."""

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import context

router = APIRouter()


# ---------------------------------------------------------------------------
# Manual recording
# ---------------------------------------------------------------------------

class RecordStartRequest(BaseModel):
    filename: Optional[str] = None


@router.post("/record/start")
async def record_start(req: RecordStartRequest = RecordStartRequest()):
    return await context.recorder.start(req.filename)


@router.post("/record/stop")
async def record_stop():
    return await context.recorder.stop()


@router.get("/record/status")
async def record_status():
    return context.recorder.status()


@router.get("/recordings")
async def get_recordings():
    return await context.recorder.list_recordings()


@router.delete("/recordings/{recording_id}", status_code=204)
async def delete_rec(recording_id: int):
    if not await context.recorder.delete_recording(recording_id):
        raise HTTPException(404, "Recording not found")


@router.get("/recordings/{recording_id}/download")
async def download_recording(recording_id: int):
    recs = await context.recorder.list_recordings()
    rec  = next((r for r in recs if r["id"] == recording_id), None)
    if not rec:
        raise HTTPException(404, "Recording not found")
    output_dir = os.path.expanduser(
        context.config.get("recordings", {}).get("output_dir", "~/recordings")
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


@router.get("/schedules")
async def get_schedules():
    return await context.recorder.list_scheduled_recordings()


@router.post("/schedules", status_code=201)
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
    return await context.recorder.create_scheduled_recording({
        "name":             req.name,
        "frequency":        req.frequency,
        "band":             req.band.lower(),
        "duration_seconds": req.duration_seconds,
        "cron_expr":        req.cron_expr,
    })


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: int):
    if not await context.recorder.delete_scheduled_recording(schedule_id):
        raise HTTPException(404, "Schedule not found")
