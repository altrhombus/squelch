"""
RadioManager — simplified coordinator for the new in-app SDR stack.

Owns:
  RadioPipeline  — pyrtlsdr + NumPy DSP + AAC encoding
  Nrsc5Backend   — HD Radio subprocess (kept; routes PCM through AacEncoder)
  StreamingManager — distributes AAC chunks to HTTP clients
  SignalLoop     — periodic metadata + signal bar broadcast

No FIFO, no ffmpeg, no HLS directories.
"""

import asyncio
import logging
import os
from typing import Optional

from scipy.signal import resample_poly

from ..metadata import MetadataState
from ..streaming import StreamingManager, AacEncoder
from .nrsc5_backend import Nrsc5Backend

logger = logging.getLogger(__name__)

# Aviation band uses AM demodulation
_AVIATION_LO = 118e6
_AVIATION_HI = 137e6


class RadioManager:
    def __init__(self, config: dict, metadata: MetadataState, streaming: StreamingManager):
        self._cfg      = config
        self._meta     = metadata
        self._streams  = streaming

        sdr_cfg = config.get("sdr", {})
        self._device_index    = sdr_cfg.get("device_index", 0)
        self._ppm             = sdr_cfg.get("ppm_correction", 0)
        self._deemphasis      = sdr_cfg.get("deemphasis_us", 75)
        self._pp_cfg          = config.get("post_processing", {})

        self._pipeline: Optional[object]      = None   # RadioPipeline
        self._nrsc5:    Optional[Nrsc5Backend] = None
        self._signal_task: Optional[asyncio.Task] = None
        self._current_freq: Optional[float] = None
        self._current_band: Optional[str]   = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def startup(self):
        pass  # nothing to pre-create

    async def tune(self, freq_hz: float, band: str, **kwargs):
        gain         = kwargs.get("gain", self._cfg.get("sdr", {}).get("gain", "auto"))
        deemph       = kwargs.get("deemphasis_us", self._deemphasis)
        stereo_mode  = kwargs.get("stereo_mode", "auto")

        self._meta.update_tune(freq_hz, band)
        await self._meta.broadcast()

        await self._stop_all()
        self._streams.drain_all()

        self._current_freq = freq_hz
        self._current_band = band

        if band == "hd":
            await self._start_hd(freq_hz)
        else:
            await self._start_pipeline(freq_hz, band, gain, deemph, stereo_mode)

        self._meta.update_state("buffering")
        await self._meta.broadcast()

        if not self._signal_task or self._signal_task.done():
            self._signal_task = asyncio.create_task(self._signal_loop())

        logger.info("Tuned to %.3f MHz [%s]", freq_hz / 1e6, band)

    async def retune_same_band(self, freq_hz: float):
        """Change frequency without restarting the SDR device (FM only)."""
        if self._current_band == "fm" and self._pipeline:
            self._meta.update_tune(freq_hz, "fm")
            await self._pipeline.retune(freq_hz)
            self._current_freq = freq_hz
        else:
            await self.tune(freq_hz, self._current_band)

    def set_squelch(self, slider: int):
        """Map UI slider (0-100) to an IQ RMS threshold. 0 = disabled."""
        threshold = slider * 0.002 if slider > 0 else 0.0
        if self._pipeline:
            self._pipeline._squelch_iq = threshold
        logger.info("Squelch threshold: %.3f (slider=%d)", threshold, slider)

    async def stop(self):
        await self._stop_all()
        if self._signal_task:
            self._signal_task.cancel()
            self._signal_task = None
        self._meta.update_state("idle")

    def status(self) -> dict:
        return {
            "running": self._pipeline is not None or self._nrsc5 is not None,
            "frequency": self._current_freq,
            "band": self._current_band,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def set_pp_bypass(self, bypass: bool):
        """Toggle post-processing bypass at runtime for A/B comparison."""
        if self._pipeline and self._pipeline._demod is not None:
            self._pipeline._demod.pp_bypass = bypass

    async def _start_pipeline(self, freq_hz: float, band: str, gain, deemph: int, stereo_mode: str = "auto"):
        from ..sdr.pipeline import RadioPipeline
        self._pipeline = RadioPipeline(self._cfg, self._meta, self._streams)
        await self._pipeline.start(freq_hz, band, gain=gain, deemphasis_us=deemph,
                                   stereo_mode=stereo_mode, post_processing=self._pp_cfg)

    async def _start_hd(self, freq_hz: float):
        encoder = AacEncoder(stereo=True)
        _live = [False]  # mutable flag for the pcm_cb closure

        def meta_cb(data: dict):
            self._meta.update_nrsc5(**data)

        def pcm_cb(pcm_l: object, pcm_r: object):
            import numpy as np
            # Transition to "live" on the first audio chunk so the frontend
            # stops showing "Buffering…" and displays station metadata instead.
            if not _live[0]:
                _live[0] = True
                self._meta.update_state("live")
                asyncio.ensure_future(self._meta.broadcast())
            # nrsc5 outputs 44100 Hz; resample to 48000 Hz (ratio 160/147) so the
            # AacEncoder (initialized at 48000 Hz) gets correctly-timed samples.
            l = resample_poly(np.asarray(pcm_l, np.float32), 160, 147).astype(np.float32)
            r = resample_poly(np.asarray(pcm_r, np.float32), 160, 147).astype(np.float32)
            chunk = encoder.encode(l, r)
            if chunk:
                self._streams.broadcast(chunk)

        self._nrsc5 = Nrsc5Backend(
            device_index=self._device_index,
            metadata_callback=meta_cb,
            pcm_callback=pcm_cb,
        )
        await self._nrsc5.start(freq_hz)

    async def _stop_all(self):
        if self._pipeline:
            await self._pipeline.stop()
            self._pipeline = None
        if self._nrsc5:
            await self._nrsc5.stop()
            self._nrsc5 = None

    # ------------------------------------------------------------------
    # Signal loop — broadcasts metadata + signal bars every second
    # ------------------------------------------------------------------

    async def _signal_loop(self):
        try:
            while True:
                await asyncio.sleep(1)
                bars, stereo = self._estimate_signal()
                self._meta.update_signal(bars, stereo)
                await self._meta.broadcast()
        except asyncio.CancelledError:
            pass

    def _estimate_signal(self) -> tuple[int, bool]:
        """
        Returns (signal_bars 0-5, stereo_active).

        Bars reflect IQ RMS — the actual RF/antenna signal level, analogous
        to RSSI on a phone.  The gain controller keeps iq_rms in [0.07, 0.38]
        for a receivable station, so thresholds are calibrated around that range.

        Stereo is driven separately by pilot_rms (19 kHz stereo pilot tone).
        HD: 5 if locked, 2 if decoding.
        """
        if self._meta.state not in ("buffering", "live"):
            return 0, False

        if self._nrsc5 and self._nrsc5.is_running():
            return (5 if self._meta.hd_locked else 2), False

        if self._pipeline:
            iq = self._pipeline.signal_strength   # IQ RMS — RF level at the antenna
            if   iq > 0.28: bars = 5
            elif iq > 0.15: bars = 4
            elif iq > 0.08: bars = 3
            elif iq > 0.04: bars = 2
            else:           bars = 1

            # Stereo badge uses pilot_rms — only meaningful for FM
            stereo = (self._current_band == "fm"
                      and self._pipeline.signal_quality > 0.05)
            return bars, stereo

        return 0, False
