"""
nrsc5 backend for HD Radio (NRSC-5) reception.

Requires: nrsc5
  sudo apt-get install nrsc5   (or build from source: https://github.com/theori-io/nrsc5)

Audio pipeline:  nrsc5 → PCM WAV pipe → write to FIFO
Metadata:        nrsc5 stdout → line parser → metadata callbacks
"""

import asyncio
import logging
import os
import re
import tempfile
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# nrsc5 stdout patterns
_RE_STATION = re.compile(r"Station name: (.+)")
_RE_SLOGAN = re.compile(r"Slogan: (.+)")
_RE_TITLE = re.compile(r"Title: (.+)")
_RE_ARTIST = re.compile(r"Artist: (.+)")
_RE_PTY = re.compile(r"Program type: (.+)")
_RE_LOT = re.compile(r"LOT file: (.+\.jpg|.+\.png)", re.IGNORECASE)
_RE_LOCKED = re.compile(r"Synchronized")
_RE_LOST = re.compile(r"Lost synchronization")


class Nrsc5Backend:
    """
    Spawns nrsc5 to decode HD Radio audio and metadata.
    Audio is written to `fifo_path` as raw PCM (44100 Hz, stereo, s16le).
    Calls `metadata_callback` with a dict of updated fields when data arrives.
    """

    def __init__(
        self,
        fifo_path: str,
        device_index: int = 0,
        metadata_callback: Optional[Callable[[dict], None]] = None,
    ):
        self._fifo_path = fifo_path
        self._device_index = device_index
        self._metadata_callback = metadata_callback
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._art_dir = tempfile.mkdtemp(prefix="squelch-art-")

    async def start(self, freq_hz: float, program: int = 0):
        await self.stop()
        # nrsc5 expects frequency in MHz
        freq_mhz = freq_hz / 1e6
        cmd = [
            "nrsc5",
            "-d", str(self._device_index),
            "-o", self._fifo_path,   # pipe raw PCM to FIFO
            "--dump-aas-files", self._art_dir,
            str(freq_mhz),
            str(program),
        ]
        logger.info("Starting nrsc5: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stdout_task = asyncio.create_task(self._parse_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self):
        for task in (self._stdout_task, self._stderr_task):
            if task:
                task.cancel()
        self._stdout_task = None
        self._stderr_task = None
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            self._process = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _parse_stdout(self):
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                self._handle_line(text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("nrsc5 stdout parser: %s", e)

    def _handle_line(self, line: str):
        if not self._metadata_callback:
            return
        update = {}
        if m := _RE_STATION.search(line):
            update["station_name"] = m.group(1).strip()
        if m := _RE_SLOGAN.search(line):
            update["slogan"] = m.group(1).strip()
        if m := _RE_TITLE.search(line):
            update["title"] = m.group(1).strip()
        if m := _RE_ARTIST.search(line):
            update["artist"] = m.group(1).strip()
        if m := _RE_PTY.search(line):
            update["pty"] = m.group(1).strip()
        if m := _RE_LOT.search(line):
            art_path = m.group(1).strip()
            if os.path.exists(art_path):
                update["art_path"] = art_path
        if _RE_LOCKED.search(line):
            update["hd_locked"] = True
        if _RE_LOST.search(line):
            update["hd_locked"] = False
        if update:
            self._metadata_callback(update)

    async def _drain_stderr(self):
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                logger.debug("nrsc5: %s", line.decode(errors="replace").strip())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("nrsc5 stderr: %s", e)
