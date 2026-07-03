"""Tuning, status, and squelch."""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import context

logger = logging.getLogger(__name__)
router = APIRouter()

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


class TuneRequest(BaseModel):
    frequency:   float
    band:        str
    gain:        Optional[str] = None
    stereo_mode: Optional[str] = None
    hd_channel:  Optional[int] = None   # 1-based HD sub-channel (HD1=1, HD2=2, …)


@router.post("/tune")
async def tune(req: TuneRequest):
    band = req.band.lower()
    if band not in ("fm", "am", "scanner", "hd", "wx"):
        raise HTTPException(400, "band must be fm, am, scanner, hd, or wx")

    freq_hz = req.frequency * 1e6 if band != "am" else req.frequency * 1e3

    kwargs: dict = {}
    if req.gain        is not None: kwargs["gain"]       = req.gain
    if req.stereo_mode is not None: kwargs["stereo_mode"] = req.stereo_mode
    if req.hd_channel  is not None: kwargs["hd_channel"] = req.hd_channel

    _spawn_bg(context.radio.tune(freq_hz, band, **kwargs))
    return {"status": "tuning", "frequency": req.frequency, "band": band}


@router.get("/status")
async def status():
    return {**context.meta.to_dict(), **context.radio.status()}


class SquelchRequest(BaseModel):
    slider: int = 0    # 0–100; 0 disables squelch


@router.post("/squelch")
async def set_squelch(req: SquelchRequest):
    slider = max(0, min(100, req.slider))
    context.radio.set_squelch(slider)
    return {"slider": slider}
