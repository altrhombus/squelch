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
        self._device_index = sdr_cfg.get("device_index", 0)
        self._ppm          = sdr_cfg.get("ppm_correction", 0)
        self._deemphasis   = sdr_cfg.get("deemphasis_us", 75)

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
        gain      = kwargs.get("gain", self._cfg.get("sdr", {}).get("gain", "auto"))
        deemph    = kwargs.get("deemphasis_us", self._deemphasis)

        self._meta.update_tune(freq_hz, band)
        await self._meta.broadcast()

        await self._stop_all()
        self._streams.drain_all()

        self._current_freq = freq_hz
        self._current_band = band

        if band == "hd":
            await self._start_hd(freq_hz)
        else:
            await self._start_pipeline(freq_hz, band, gain, deemph)

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

    async def _start_pipeline(self, freq_hz: float, band: str, gain, deemph: int):
        from ..sdr.pipeline import RadioPipeline
        self._pipeline = RadioPipeline(self._cfg, self._meta, self._streams)
        await self._pipeline.start(freq_hz, band, gain=gain, deemphasis_us=deemph)

    async def _start_hd(self, freq_hz: float):
        encoder = AacEncoder(stereo=True)

        def meta_cb(data: dict):
            self._meta.update_nrsc5(**data)

        def pcm_cb(pcm_l: object, pcm_r: object):
            import numpy as np
            chunk = encoder.encode(
                np.asarray(pcm_l, dtype=np.float32),
                np.asarray(pcm_r, dtype=np.float32),
            )
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
                # Populate diagnostics panel
                if self._pipeline:
                    m = self._pipeline.signal_metrics
                    m["band"] = self._current_band
                    self._meta.diag = m
                elif self._nrsc5 and self._nrsc5.is_running():
                    self._meta.diag = {"band": "hd", "hd_locked": self._meta.hd_locked}
                else:
                    self._meta.diag = {}
                await self._meta.broadcast()
        except asyncio.CancelledError:
            pass

    def _estimate_signal(self) -> tuple[int, bool]:
        """
        Returns (signal_bars 0-5, stereo_active).

        FM: pilot RMS → bars + stereo blend detection.
          pilot_rms ≈ 0.07 on a good signal (pilot is 10% of deviation,
          RMS of sine ≈ A/√2).  Blend kicks in below 0.08; full mono below 0.02.

        AM/scanner: fixed 3 bars when running (no pilot to measure).
        HD: 5 if locked, 2 if decoding.
        """
        if self._meta.state not in ("buffering", "live"):
            return 0, False

        if self._nrsc5 and self._nrsc5.is_running():
            return (5 if self._meta.hd_locked else 2), False

        if self._pipeline:
            q = self._pipeline.signal_quality   # 0.0–1.0 (pilot RMS for FM)
            if self._current_band == "fm":
                # Map pilot RMS to 1–5 bars
                if   q > 0.08: bars = 5
                elif q > 0.05: bars = 4
                elif q > 0.02: bars = 3
                elif q > 0.005: bars = 2
                else:          bars = 1
                # Stereo badge: pilot strong enough for meaningful separation
                stereo = q > 0.05
                return bars, stereo
            else:
                return 3, False   # AM/scanner: running = 3 bars, always mono

        return 0, False
