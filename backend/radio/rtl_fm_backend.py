"""
rtl_fm-based backend for AM and NFM (scanner) modes.
These are inherently mono signals — GNU Radio is not needed.

Requires: rtl-sdr (provides rtl_fm)
  sudo apt-get install rtl-sdr
"""

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class RtlFmBackend:
    """
    Spawns an `rtl_fm` subprocess and writes raw mono s16le PCM to `fifo_path`.
    Signal level is estimated from stderr output.
    """

    AUDIO_RATE_AM = 44_100
    AUDIO_RATE_NFM = 22_050

    def __init__(self, fifo_path: str, device_index: int = 0, ppm_correction: int = 0):
        self._fifo_path = fifo_path
        self._device_index = device_index
        self._ppm_correction = ppm_correction
        self._process: Optional[asyncio.subprocess.Process] = None
        self._monitor_task: Optional[asyncio.Task] = None

    async def start(self, freq_hz: float, band: str, gain="auto", deemphasis_us: int = 75):
        await self.stop()
        cmd = self._build_cmd(freq_hz, band, gain, deemphasis_us)
        logger.info("Starting rtl_fm: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=open(self._fifo_path, "wb"),
            stderr=asyncio.subprocess.PIPE,
        )
        self._monitor_task = asyncio.create_task(self._monitor_stderr())

    async def stop(self):
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None
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

    def _build_cmd(self, freq_hz: float, band: str, gain, deemphasis_us: int) -> list[str]:
        cmd = ["rtl_fm"]

        cmd += ["-d", str(self._device_index)]

        if band == "am":
            cmd += [
                "-f", str(int(freq_hz)),
                "-M", "am",
                "-s", "250000",
                "-r", str(self.AUDIO_RATE_AM),
                "-E", "deemp",
                "-D", "2",  # direct sampling for AM below ~30MHz
            ]
        elif band == "scanner":
            # Narrowband FM — aviation is actually AM, but scanner covers both
            if 118e6 <= freq_hz <= 137e6:
                # Aviation band — use AM
                cmd += [
                    "-f", str(int(freq_hz)),
                    "-M", "am",
                    "-s", "250000",
                    "-r", str(self.AUDIO_RATE_NFM),
                ]
            else:
                cmd += [
                    "-f", str(int(freq_hz)),
                    "-M", "fm",
                    "-s", "200000",
                    "-r", str(self.AUDIO_RATE_NFM),
                ]
        else:
            raise ValueError(f"rtl_fm backend does not support band: {band}")

        if self._ppm_correction:
            cmd += ["-p", str(self._ppm_correction)]

        if gain != "auto":
            cmd += ["-g", str(gain)]

        cmd.append("-")  # output to stdout
        return cmd

    async def _monitor_stderr(self):
        """Read stderr to surface errors; could parse signal level in future."""
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace").strip()
                if decoded:
                    logger.debug("rtl_fm: %s", decoded)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("rtl_fm stderr monitor: %s", e)
