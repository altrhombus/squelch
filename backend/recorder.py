"""
Recording management.

A recording tees the active audio FIFO to an AAC/M4A file via ffmpeg.
Names are auto-generated from current metadata when available.
Scheduled recordings use APScheduler.
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

FIFO_PATH = "/tmp/squelch-audio.fifo"


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
    return os.path.join(os.path.expanduser(output_dir), "_".join(parts) + ".m4a")


class Recorder:
    def __init__(self, config: dict, metadata: MetadataState):
        self._cfg = config
        self._meta = metadata
        self._output_dir = config.get("recordings", {}).get("output_dir", "~/recordings")
        self._bitrate = config.get("recordings", {}).get("default_bitrate", 128)
        self._recording_proc: Optional[asyncio.subprocess.Process] = None
        self._recording_start: Optional[datetime] = None
        self._recording_id: Optional[int] = None
        self._recording_file: Optional[str] = None
        self._scheduler: Optional[AsyncIOScheduler] = None

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
        if self._recording_proc and self._recording_proc.returncode is None:
            return {"error": "already recording", "file": self._recording_file}

        out_file = filename or _auto_filename(self._meta, self._output_dir)

        is_stereo = self._meta.band in ("fm", "hd")
        channels = 2 if is_stereo else 1
        input_rate = 48_000 if self._meta.band == "fm" else 44_100

        cmd = [
            "ffmpeg", "-y",
            "-f", "s16le",
            "-ar", str(input_rate),
            "-ac", str(channels),
            "-i", FIFO_PATH,
            "-c:a", "aac",
            "-b:a", f"{self._bitrate}k",
            out_file,
        ]

        logger.info("Recording to: %s", out_file)
        self._recording_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._recording_start = datetime.now(timezone.utc)
        self._recording_file = out_file

        db = await get_db()
        try:
            cur = await db.execute(
                """INSERT INTO recordings (filename, station_name, artist, title, frequency, band)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    os.path.basename(out_file),
                    self._meta.station_name,
                    self._meta.artist,
                    self._meta.title,
                    self._meta.frequency,
                    self._meta.band,
                ),
            )
            await db.commit()
            self._recording_id = cur.lastrowid
        finally:
            await db.close()

        return {"status": "recording", "file": os.path.basename(out_file)}

    async def stop(self) -> dict:
        if not self._recording_proc:
            return {"error": "not recording"}

        try:
            self._recording_proc.terminate()
            await asyncio.wait_for(self._recording_proc.wait(), timeout=3.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                self._recording_proc.kill()
            except ProcessLookupError:
                pass

        ended_at = datetime.now(timezone.utc)
        duration = int((ended_at - self._recording_start).total_seconds())

        if self._recording_id:
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE recordings SET ended_at = ?, duration_seconds = ? WHERE id = ?",
                    (ended_at.isoformat(), duration, self._recording_id),
                )
                await db.commit()
            finally:
                await db.close()

        result = {
            "status": "stopped",
            "file": os.path.basename(self._recording_file or ""),
            "duration_seconds": duration,
        }
        self._recording_proc = None
        self._recording_start = None
        self._recording_id = None
        self._recording_file = None
        return result

    def is_recording(self) -> bool:
        return self._recording_proc is not None and self._recording_proc.returncode is None

    # ------------------------------------------------------------------
    # Recordings list
    # ------------------------------------------------------------------

    async def list_recordings(self) -> list[dict]:
        db = await get_db()
        try:
            async with db.execute(
                "SELECT * FROM recordings ORDER BY started_at DESC"
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        finally:
            await db.close()

    async def delete_recording(self, recording_id: int) -> bool:
        db = await get_db()
        try:
            async with db.execute(
                "SELECT filename FROM recordings WHERE id = ?", (recording_id,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return False
            filepath = os.path.join(
                os.path.expanduser(self._output_dir), row["filename"]
            )
            if os.path.exists(filepath):
                os.remove(filepath)
            await db.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
            await db.commit()
            return True
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Scheduled recordings
    # ------------------------------------------------------------------

    async def _load_scheduled_recordings(self):
        db = await get_db()
        try:
            async with db.execute(
                "SELECT * FROM scheduled_recordings WHERE enabled = 1"
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                self._add_scheduled_job(dict(row))
        finally:
            await db.close()

    def _add_scheduled_job(self, sched: dict):
        from apscheduler.triggers.cron import CronTrigger

        async def job():
            from .radio.manager import RadioManager  # avoid circular import
            # In a full impl the manager would be injected; this is a simplified version
            await self.start()
            await asyncio.sleep(sched["duration_seconds"])
            await self.stop()

        self._scheduler.add_job(
            job,
            CronTrigger.from_crontab(sched["cron_expr"]),
            id=f"sched_{sched['id']}",
            replace_existing=True,
        )

    async def create_scheduled_recording(self, data: dict) -> dict:
        db = await get_db()
        try:
            cur = await db.execute(
                """INSERT INTO scheduled_recordings
                   (name, frequency, band, duration_seconds, cron_expr)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    data["name"],
                    data["frequency"],
                    data["band"],
                    data["duration_seconds"],
                    data["cron_expr"],
                ),
            )
            await db.commit()
            row_id = cur.lastrowid
        finally:
            await db.close()

        db = await get_db()
        try:
            async with db.execute(
                "SELECT * FROM scheduled_recordings WHERE id = ?", (row_id,)
            ) as cur:
                row = await cur.fetchone()
                sched = dict(row)
        finally:
            await db.close()

        self._add_scheduled_job(sched)
        return sched
