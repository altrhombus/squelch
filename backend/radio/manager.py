"""
RadioManager — unified interface for tuning, streaming, and signal control.

Manages:
- Backend selection (GNU Radio FM, rtl_fm AM/Scanner, nrsc5 HD)
- A single named FIFO that the ffmpeg HLS process reads
- ffmpeg process that converts FIFO PCM → HLS segments
- Signal level estimation (updated on a timer)
"""

import asyncio
import logging
import os
import shutil
import stat
from typing import Optional

from ..metadata import MetadataState

logger = logging.getLogger(__name__)

FIFO_PATH = "/tmp/squelch-audio.fifo"


class RadioManager:
    def __init__(self, config: dict, metadata: MetadataState):
        self._cfg = config
        self._meta = metadata
        self._current_band: Optional[str] = None
        self._current_freq: Optional[float] = None
        self._ffmpeg_proc: Optional[asyncio.subprocess.Process] = None
        self._signal_task: Optional[asyncio.Task] = None

        sdr_cfg = config.get("sdr", {})
        self._device_index = sdr_cfg.get("device_index", 0)
        self._ppm = sdr_cfg.get("ppm_correction", 0)
        self._deemphasis = sdr_cfg.get("deemphasis_us", 75)

        hls_cfg = config.get("hls", {})
        self._segment_dir = hls_cfg.get("segment_dir", "/tmp/sdr-hls")
        self._segment_dur = hls_cfg.get("segment_duration", 3)
        self._playlist_size = hls_cfg.get("playlist_size", 10)

        self._gnu_radio: Optional[object] = None
        self._rtl_fm: Optional[object] = None
        self._nrsc5: Optional[object] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def startup(self):
        self._ensure_fifo()
        os.makedirs(self._segment_dir, exist_ok=True)

    async def tune(self, freq_hz: float, band: str, **kwargs):
        gain = kwargs.get("gain", self._cfg.get("sdr", {}).get("gain", "auto"))
        bandwidth = kwargs.get("bandwidth", "wide")
        stereo_mode = kwargs.get("stereo_mode", "auto")

        self._meta.update_tune(freq_hz, band)
        await self._stop_all_backends()

        # Clear old HLS segments so the player starts fresh
        self._clear_hls_dir()
        self._ensure_fifo()

        if band == "fm":
            await self._start_fm(freq_hz, gain, bandwidth, stereo_mode)
        elif band == "hd":
            await self._start_hd(freq_hz)
        elif band in ("am", "scanner"):
            await self._start_rtl_fm(freq_hz, band, gain)
        else:
            raise ValueError(f"Unknown band: {band}")

        self._current_freq = freq_hz
        self._current_band = band

        # Restart ffmpeg reading from FIFO
        await self._start_ffmpeg(band)

        if not self._signal_task or self._signal_task.done():
            self._signal_task = asyncio.create_task(self._signal_loop())

        logger.info("Tuned to %.3f MHz [%s]", freq_hz / 1e6, band)

    async def retune(self, freq_hz: float):
        """Change frequency within the same band without full restart."""
        if self._current_band == "fm" and self._gnu_radio:
            self._gnu_radio.tune(freq_hz)
            self._current_freq = freq_hz
            self._meta.update_tune(freq_hz, self._current_band)
        elif self._current_band in ("am", "scanner"):
            await self.tune(freq_hz, self._current_band)
        elif self._current_band == "hd":
            await self.tune(freq_hz, "hd")

    async def set_gain(self, gain):
        self._cfg.setdefault("sdr", {})["gain"] = gain
        if self._gnu_radio:
            self._gnu_radio.set_gain(gain)

    async def set_stereo_mode(self, mode: str):
        if self._gnu_radio:
            self._gnu_radio.set_stereo_mode(mode)

    async def stop(self):
        await self._stop_all_backends()
        await self._stop_ffmpeg()
        if self._signal_task:
            self._signal_task.cancel()
            self._signal_task = None

    def status(self) -> dict:
        return {
            "running": self._ffmpeg_proc is not None
            and self._ffmpeg_proc.returncode is None,
            "frequency": self._current_freq,
            "band": self._current_band,
        }

    # ------------------------------------------------------------------
    # Backend lifecycle
    # ------------------------------------------------------------------

    async def _start_fm(self, freq_hz, gain, bandwidth, stereo_mode):
        from .gnu_radio_fm import GnuRadioFM

        def rds_cb(data: dict):
            self._meta.update_rds(
                ps=data.get("ps"),
                rt=data.get("rt"),
                pty=data.get("pty"),
                pi=data.get("pi"),
            )

        gr = GnuRadioFM(
            fifo_path=FIFO_PATH,
            device_index=self._device_index,
            ppm_correction=self._ppm,
            deemphasis_us=self._deemphasis,
            rds_callback=rds_cb,
        )
        gr.start(freq_hz, gain=gain, bandwidth=bandwidth)
        self._gnu_radio = gr

    async def _start_hd(self, freq_hz):
        from .nrsc5_backend import Nrsc5Backend

        def meta_cb(data: dict):
            self._meta.update_nrsc5(**data)

        n = Nrsc5Backend(
            fifo_path=FIFO_PATH,
            device_index=self._device_index,
            metadata_callback=meta_cb,
        )
        await n.start(freq_hz)
        self._nrsc5 = n

    async def _start_rtl_fm(self, freq_hz, band, gain):
        from .rtl_fm_backend import RtlFmBackend

        r = RtlFmBackend(
            fifo_path=FIFO_PATH,
            device_index=self._device_index,
            ppm_correction=self._ppm,
        )
        await r.start(freq_hz, band, gain, self._deemphasis)
        self._rtl_fm = r

    async def _stop_all_backends(self):
        if self._gnu_radio:
            self._gnu_radio.stop()
            self._gnu_radio = None
        if self._rtl_fm:
            await self._rtl_fm.stop()
            self._rtl_fm = None
        if self._nrsc5:
            await self._nrsc5.stop()
            self._nrsc5 = None

    # ------------------------------------------------------------------
    # ffmpeg HLS process
    # ------------------------------------------------------------------

    async def _start_ffmpeg(self, band: str):
        await self._stop_ffmpeg()

        is_stereo = band in ("fm", "hd")
        channels = 2 if is_stereo else 1
        bitrate = "128k" if is_stereo else "32k"

        # GNU Radio FM outputs 50000 Hz (2MHz / 10 / 4), nrsc5 outputs 44100,
        # rtl_fm outputs 44100 (AM) or 22050 (NFM scanner)
        if band == "fm":
            input_rate = 50_000
        elif band == "scanner":
            input_rate = 22_050
        else:
            input_rate = 44_100

        m3u8_path = os.path.join(self._segment_dir, "stream.m3u8")

        cmd = [
            "ffmpeg", "-y",
            "-f", "s16le",
            "-ar", str(input_rate),
            "-ac", str(channels),
            "-i", FIFO_PATH,
            "-c:a", "aac",
            "-b:a", bitrate,
            "-filter:a", "dynaudnorm",
            "-f", "hls",
            "-hls_time", str(self._segment_dur),
            "-hls_list_size", str(self._playlist_size),
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", os.path.join(self._segment_dir, "seg%05d.ts"),
            m3u8_path,
        ]

        logger.info("Starting ffmpeg HLS: %s", " ".join(cmd))
        self._ffmpeg_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _stop_ffmpeg(self):
        if self._ffmpeg_proc:
            try:
                self._ffmpeg_proc.terminate()
                await asyncio.wait_for(self._ffmpeg_proc.wait(), timeout=3.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._ffmpeg_proc.kill()
                except ProcessLookupError:
                    pass
            self._ffmpeg_proc = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_fifo(self):
        if os.path.exists(FIFO_PATH):
            if not stat.S_ISFIFO(os.stat(FIFO_PATH).st_mode):
                os.remove(FIFO_PATH)
                os.mkfifo(FIFO_PATH)
        else:
            os.mkfifo(FIFO_PATH)

    def _clear_hls_dir(self):
        for name in os.listdir(self._segment_dir):
            if name.endswith((".ts", ".m3u8")):
                try:
                    os.remove(os.path.join(self._segment_dir, name))
                except OSError:
                    pass

    async def _signal_loop(self):
        """Estimate signal bars from process liveness + basic heuristics."""
        try:
            while True:
                await asyncio.sleep(2)
                bars = self._estimate_signal_bars()
                stereo = self._current_band == "fm"
                self._meta.update_signal(bars, stereo)
                await self._meta.broadcast()
        except asyncio.CancelledError:
            pass

    def _estimate_signal_bars(self) -> int:
        # Placeholder: 3 bars when any backend is alive, 0 otherwise.
        # A real implementation would parse rtl_fm gain output or use
        # osmosdr's get_sensor("rssi") via GNU Radio.
        if self._gnu_radio is not None:
            return 3
        if self._rtl_fm is not None and self._rtl_fm.is_running():
            return 3
        if self._nrsc5 is not None and self._nrsc5.is_running():
            return 4 if self._meta.hd_locked else 2
        return 0
