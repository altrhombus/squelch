"""
Recording management.

Recordings are made by subscribing to the StreamingManager's AAC queue
and writing the raw ADTS-framed bytes directly to a .aac file.
No ffmpeg subprocess needed — the AAC is already encoded by the pipeline.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .db import get_db
from .metadata import MetadataState

logger = logging.getLogger(__name__)


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w\-]", "_", s)[:40]


def _auto_filename(meta: MetadataState, output_dir: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    parts = [ts]
    if meta.station_name:
        parts.append(_safe_name(meta.station_name))
    if meta.artist and meta.title:
        parts.append(_safe_name(f"{meta.artist}-{meta.title}"))
    elif meta.title:
        parts.append(_safe_name(meta.title))
    return os.path.join(os.path.expanduser(output_dir), "_".join(parts) + ".aac")


class Recorder:
    def __init__(self, config: dict, metadata: MetadataState, streaming=None):
        self._cfg         = config
        self._meta        = metadata
        self._streams     = streaming           # StreamingManager (injected later)
        self._output_dir  = config.get("recordings", {}).get("output_dir", "~/recordings")
        self._rec_task:   Optional[asyncio.Task]  = None
        self._rec_queue:  Optional[asyncio.Queue] = None
        self._rec_file:   Optional[object]        = None
        self._rec_start:  Optional[datetime]      = None
        self._rec_id:     Optional[int]           = None
        self._recording_file: Optional[str]       = None
        self._scheduler:  Optional[AsyncIOScheduler] = None
        self._radio       = None                # RadioManager (injected later)

    def set_streaming(self, streaming):
        self._streams = streaming

    def set_radio(self, radio):
        self._radio = radio

    async def startup(self):
        os.makedirs(os.path.expanduser(self._output_dir), exist_ok=True)
        self._scheduler = AsyncIOScheduler()
        await self._load_scheduled_recordings()
        self._scheduler.start()

    async def shutdown(self):
        await self.stop()
        if self._scheduler:
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Manual recording
    # ------------------------------------------------------------------

    async def start(self, filename: Optional[str] = None) -> dict:
        if self._rec_task and not self._rec_task.done():
            return {"error": "already recording", "file": self._recording_file}
        if not self._streams:
            return {"error": "streaming not available"}

        if filename:
            # Basename + pin to output_dir — a client-supplied filename must not
            # be able to write outside the recordings directory.
            safe = os.path.basename(filename.strip())
            if not safe or safe.startswith("."):
                return {"error": "invalid filename"}
            out_file = os.path.join(os.path.expanduser(self._output_dir), safe)
        else:
            out_file = _auto_filename(self._meta, self._output_dir)
        self._recording_file = out_file
        self._rec_start      = datetime.now(timezone.utc)
        self._rec_queue      = self._streams.new_client()
        self._rec_file       = open(out_file, "wb")

        db = await get_db()
        cur = await db.execute(
            """INSERT INTO recordings
                   (filename, station_name, artist, title, frequency, band, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (os.path.basename(out_file), self._meta.station_name,
             self._meta.artist, self._meta.title,
             self._meta.frequency, self._meta.band,
             self._rec_start.isoformat()),
        )
        await db.commit()
        self._rec_id = cur.lastrowid

        self._rec_task = asyncio.create_task(self._write_loop())
        logger.info("Recording to: %s", out_file)
        return {
            "status": "recording",
            "file": os.path.basename(out_file),
            "started_at": self._rec_start.timestamp(),
        }

    async def stop(self) -> dict:
        # Clean up even if the write task already died on its own — the open
        # file handle and DB row must not be left dangling.
        if not self._rec_task and not self._rec_file:
            return {"error": "not recording"}

        if self._rec_task:
            self._rec_task.cancel()
            try:
                await self._rec_task
            except (asyncio.CancelledError, Exception):
                pass
            self._rec_task = None

        if self._streams and self._rec_queue:
            self._streams.remove_client(self._rec_queue)
        self._rec_queue = None

        if self._rec_file:
            self._rec_file.close()
            self._rec_file = None

        ended_at = datetime.now(timezone.utc)
        duration = int((ended_at - self._rec_start).total_seconds()) if self._rec_start else 0

        if self._rec_id:
            db = await get_db()
            await db.execute(
                "UPDATE recordings SET ended_at=?, duration_seconds=? WHERE id=?",
                (ended_at.isoformat(), duration, self._rec_id),
            )
            await db.commit()

        result = {
            "status": "stopped",
            "file": os.path.basename(self._recording_file or ""),
            "duration_seconds": duration,
        }
        self._recording_file = self._rec_id = self._rec_start = None
        return result

    def is_recording(self) -> bool:
        return self._rec_task is not None and not self._rec_task.done()

    def status(self) -> dict:
        result = {
            "recording": self.is_recording(),
            "file": self._recording_file and os.path.basename(self._recording_file),
        }
        if self.is_recording() and self._rec_start is not None:
            result["started_at"] = self._rec_start.timestamp()
        return result

    async def _write_loop(self):
        # No timeout: a recording survives stream stalls and retune drains,
        # and ends only when stop() cancels this task.
        try:
            while True:
                chunk = await self._rec_queue.get()
                if self._rec_file and chunk:
                    self._rec_file.write(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Recording write error: %s", e)

    # ------------------------------------------------------------------
    # Recordings list
    # ------------------------------------------------------------------

    async def list_recordings(self) -> list[dict]:
        db = await get_db()
        async with db.execute("SELECT * FROM recordings ORDER BY started_at DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def delete_recording(self, recording_id: int) -> bool:
        db = await get_db()
        async with db.execute("SELECT filename FROM recordings WHERE id=?", (recording_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        path = os.path.join(os.path.expanduser(self._output_dir), row["filename"])
        if os.path.exists(path):
            os.remove(path)
        await db.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
        await db.commit()
        return True

    # ------------------------------------------------------------------
    # Scheduled recordings
    # ------------------------------------------------------------------

    async def _load_scheduled_recordings(self):
        db = await get_db()
        async with db.execute("SELECT * FROM scheduled_recordings WHERE enabled=1") as cur:
            rows = await cur.fetchall()
        for row in rows:
            self._add_scheduled_job(dict(row))

    def _add_scheduled_job(self, sched: dict):
        from apscheduler.triggers.cron import CronTrigger

        async def job():
            await self._run_scheduled(sched)

        self._scheduler.add_job(
            job, CronTrigger.from_crontab(sched["cron_expr"]),
            id=f"sched_{sched['id']}", replace_existing=True,
        )

    async def _run_scheduled(self, sched: dict):
        if self.is_recording():
            logger.warning("Scheduled recording %r skipped — already recording", sched["name"])
            return
        if not self._radio:
            logger.warning("Scheduled recording %r skipped — radio not available", sched["name"])
            return

        band    = sched["band"]
        # frequency is stored in display units (MHz, or kHz for AM) like presets
        freq_hz = sched["frequency"] * (1e3 if band == "am" else 1e6)
        prev    = self._radio.status()   # station to restore afterwards

        logger.info("Scheduled recording %r: tuning %.4g %s [%s] for %ds",
                    sched["name"], sched["frequency"],
                    "kHz" if band == "am" else "MHz", band,
                    sched["duration_seconds"])
        await self._radio.tune(freq_hz, band)
        await asyncio.sleep(2)   # let the gain controller settle before capture

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        await self.start(f"{ts}_{_safe_name(sched['name'])}.aac")
        try:
            await asyncio.sleep(sched["duration_seconds"])
        finally:
            await self.stop()

        if prev["frequency"] and (prev["frequency"], prev["band"]) != (freq_hz, band):
            await self._radio.tune(prev["frequency"], prev["band"])

    async def create_scheduled_recording(self, data: dict) -> dict:
        db = await get_db()
        cur = await db.execute(
            """INSERT INTO scheduled_recordings
               (name, frequency, band, duration_seconds, cron_expr)
               VALUES (?, ?, ?, ?, ?)""",
            (data["name"], data["frequency"], data["band"],
             data["duration_seconds"], data["cron_expr"]),
        )
        await db.commit()
        async with db.execute(
            "SELECT * FROM scheduled_recordings WHERE id=?", (cur.lastrowid,)
        ) as row_cur:
            sched = dict(await row_cur.fetchone())
        self._add_scheduled_job(sched)
        return sched

    async def list_scheduled_recordings(self) -> list[dict]:
        db = await get_db()
        async with db.execute("SELECT * FROM scheduled_recordings ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def delete_scheduled_recording(self, sched_id: int) -> bool:
        try:
            self._scheduler.remove_job(f"sched_{sched_id}")
        except Exception:
            pass   # job may not exist (disabled schedule)
        db = await get_db()
        cur = await db.execute("DELETE FROM scheduled_recordings WHERE id=?", (sched_id,))
        await db.commit()
        return cur.rowcount > 0
