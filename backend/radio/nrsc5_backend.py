"""
nrsc5 backend for HD Radio (NRSC-5) reception.

Requires: nrsc5 (built from source — not in Raspbian apt)
  https://github.com/theori-io/nrsc5

Audio pipeline:
  nrsc5 writes s16le stereo 44100 Hz PCM → write end of os.pipe()
  Python reads from read end → float32 frames → pcm_callback(l, r)

Metadata:
  nrsc5 text output → stdout → line parser → metadata_callback({...})
"""

import asyncio
import logging
import os
import re
import struct
import tempfile
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

_PCM_RATE    = 44_100
_PCM_CHUNK   = 4096          # bytes per read (~23 ms of stereo s16le at 44100)
_S16_SCALE   = 1.0 / 32768.0

_RE_STATION = re.compile(r"Station name: (.+)")
_RE_SLOGAN  = re.compile(r"Slogan: (.+)")
_RE_TITLE   = re.compile(r"Title: (.+)")
_RE_ARTIST  = re.compile(r"Artist: (.+)")
_RE_PTY     = re.compile(r"Program type: (.+)")
_RE_LOT     = re.compile(r"LOT file: (.+\.jpg|.+\.png)", re.IGNORECASE)
_RE_LOCKED       = re.compile(r"Synchronized")
_RE_LOST         = re.compile(r"Lost synchronization")
_RE_PROGRAM_AVAIL = re.compile(r"Audio program (\d+):")


class Nrsc5Backend:
    """
    Spawns nrsc5 and bridges its PCM output to a Python callback.
    Audio arrives as (left, right) float32 arrays at 44100 Hz.
    """

    def __init__(
        self,
        device_index: int = 0,
        metadata_callback: Optional[Callable[[dict], None]] = None,
        pcm_callback: Optional[Callable] = None,
    ):
        self._device_index    = device_index
        self._metadata_cb     = metadata_callback
        self._pcm_cb          = pcm_callback
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_task:    Optional[asyncio.Task] = None
        self._stderr_task:    Optional[asyncio.Task] = None
        self._pcm_task:       Optional[asyncio.Task] = None
        self._art_task:       Optional[asyncio.Task] = None
        self._art_dir         = tempfile.mkdtemp(prefix="squelch-art-")
        self._pcm_r_fd:       Optional[int] = None
        self._pcm_w_fd:       Optional[int] = None
        self._available_programs: set = set()   # 0-based program numbers seen in output

    async def start(self, freq_hz: float, program: int = 0):
        await self.stop()
        self._available_programs = set()

        # Anonymous pipe: nrsc5 writes PCM to w_fd, Python reads from r_fd
        self._pcm_r_fd, self._pcm_w_fd = os.pipe()
        pcm_path = f"/dev/fd/{self._pcm_w_fd}"

        freq_mhz = freq_hz / 1e6
        cmd = [
            "nrsc5",
            "-d", str(self._device_index),
            "-o", pcm_path,
            "--dump-aas-files", self._art_dir,
            str(freq_mhz),
            str(program),
        ]
        logger.info("Starting nrsc5: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(self._pcm_w_fd,),
        )
        # Close the write end in the parent — only the child should write
        os.close(self._pcm_w_fd)
        self._pcm_w_fd = None

        self._stdout_task = asyncio.create_task(self._parse_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._pcm_task    = asyncio.create_task(self._read_pcm())
        self._art_task    = asyncio.create_task(self._watch_art_dir())

    async def stop(self):
        for task in (self._stdout_task, self._stderr_task, self._pcm_task, self._art_task):
            if task:
                task.cancel()
        self._stdout_task = self._stderr_task = self._pcm_task = self._art_task = None

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

        for fd in (self._pcm_r_fd, self._pcm_w_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._pcm_r_fd = self._pcm_w_fd = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ------------------------------------------------------------------

    async def _read_pcm(self):
        if self._pcm_r_fd is None or self._pcm_cb is None:
            return
        loop = asyncio.get_event_loop()
        try:
            while True:
                # Read from the pipe in a thread to avoid blocking the event loop
                raw = await loop.run_in_executor(
                    None, os.read, self._pcm_r_fd, _PCM_CHUNK
                )
                if not raw:
                    break
                # s16le stereo → float32 L/R
                n_samples = len(raw) // 4  # 2 channels × 2 bytes
                if n_samples == 0:
                    continue
                arr = np.frombuffer(raw[:n_samples * 4], dtype=np.int16).astype(np.float32)
                arr *= _S16_SCALE
                l = arr[0::2]
                r = arr[1::2]
                self._pcm_cb(l, r)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("nrsc5 PCM reader: %s", e)

    async def _parse_stdout(self):
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                self._handle_line(line.decode(errors="replace").strip())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("nrsc5 stdout: %s", e)

    def _handle_line(self, line: str):
        if not self._metadata_cb:
            return
        update: dict = {}
        if m := _RE_STATION.search(line): update["station_name"] = m.group(1).strip()
        if m := _RE_SLOGAN.search(line):  update["slogan"]       = m.group(1).strip()
        if m := _RE_TITLE.search(line):   update["title"]        = m.group(1).strip()
        if m := _RE_ARTIST.search(line):  update["artist"]       = m.group(1).strip()
        if m := _RE_PTY.search(line):     update["pty"]          = m.group(1).strip()
        if m := _RE_LOT.search(line):
            art = m.group(1).strip()
            # nrsc5 may log only the filename; resolve to the full art dir path
            if not os.path.isabs(art):
                art = os.path.join(self._art_dir, art)
            logger.info("nrsc5 LOT file: %s (exists=%s)", art, os.path.exists(art))
            if os.path.exists(art):
                update["art_path"] = art
        if _RE_LOCKED.search(line): update["hd_locked"] = True
        if _RE_LOST.search(line):   update["hd_locked"] = False
        if m := _RE_PROGRAM_AVAIL.search(line):
            self._available_programs.add(int(m.group(1)))
            # Broadcast as 1-based channel numbers (HD1, HD2, …)
            update["hd_channels_available"] = sorted(p + 1 for p in self._available_programs)
        if update:
            self._metadata_cb(update)

    async def _watch_art_dir(self):
        """Poll the art directory for new image files every 3 seconds.

        Belt-and-suspenders fallback for when the LOT line is missed or arrives
        before the file is fully written. Each file is delivered only once.
        """
        seen: set[str] = set()
        try:
            while True:
                await asyncio.sleep(3)
                if not self._metadata_cb or not os.path.isdir(self._art_dir):
                    continue
                for fname in sorted(os.listdir(self._art_dir)):
                    if fname in seen:
                        continue
                    if fname.lower().endswith((".jpg", ".jpeg", ".png")):
                        fpath = os.path.join(self._art_dir, fname)
                        if os.path.getsize(fpath) > 0:
                            seen.add(fname)
                            logger.info("nrsc5 art dir: new file %s", fname)
                            self._metadata_cb({"art_path": fpath})
        except asyncio.CancelledError:
            pass

    async def _drain_stderr(self):
        """Read stderr and parse it for metadata — nrsc5 sends informational
        output (Station name, Synchronized, Title, …) to stderr on most builds."""
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").rstrip()
                logger.debug("nrsc5: %s", decoded)
                self._handle_line(decoded)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("nrsc5 stderr error: %s", e)
