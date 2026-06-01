"""
RadioPipeline — owns the RTL-SDR device, DSP, and feeds encoded AAC
chunks to the StreamingManager.

FM / HD: 2.4 MHz sample rate, stereo
AM / Scanner: 1.2 MHz sample rate, mono, AM uses direct sampling
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
from scipy.signal import hilbert

from .fm import FmStereoDemodulator
from .am import AmDemodulator, NfmDemodulator
from .rds import RdsDecoder

logger = logging.getLogger(__name__)

_FM_SR   = 2_400_000
_AM_SR   = 1_200_000
_BLOCK   = 131_072      # IQ samples per SDR read (≈ 55 ms at 2.4 MHz)
_AM_BLOCK = 65_536      # IQ samples per read at 1.2 MHz (≈ 55 ms)

# Aviation band uses AM demodulation even though it's a "scanner" band
_AVIATION_LO = 118e6
_AVIATION_HI = 137e6


class RadioPipeline:
    """
    Manages the full path from RTL-SDR IQ samples to AAC audio chunks.

    Usage:
        pipeline = RadioPipeline(config, metadata, streaming_manager)
        await pipeline.start(freq_hz, band)
        ...
        await pipeline.stop()
        await pipeline.start(new_freq, band)  # retune
    """

    def __init__(self, config: dict, metadata, streaming_manager):
        self._cfg      = config
        self._meta     = metadata
        self._streams  = streaming_manager
        self._sdr      = None
        self._task: Optional[asyncio.Task] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sdr-dsp")
        self._band: Optional[str] = None
        self._freq: Optional[float] = None
        self._demod = None
        self._rds:  Optional[RdsDecoder] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, freq_hz: float, band: str, gain="auto", deemphasis_us: int = 75):
        await self.stop()
        self._band = band
        self._freq = freq_hz
        self._demod = self._make_demod(band, freq_hz, deemphasis_us)

        if band == "fm":
            self._rds = RdsDecoder(self._on_rds)
        else:
            self._rds = None

        self._task = asyncio.create_task(
            self._run(freq_hz, band, gain),
            name=f"sdr-{band}-{freq_hz/1e6:.3f}MHz",
        )

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._demod = None
        self._rds   = None

    async def retune(self, freq_hz: float):
        """Change frequency within the same band without full restart."""
        if self._band == "fm" and self._sdr is not None:
            try:
                self._sdr.center_freq = freq_hz
                self._freq = freq_hz
                self._meta.update_tune(freq_hz, self._band)
                if self._rds:
                    self._rds = RdsDecoder(self._on_rds)
                logger.info("Retuned to %.3f MHz [%s]", freq_hz / 1e6, self._band)
            except Exception as e:
                logger.warning("Retune failed, restarting: %s", e)
                await self.start(freq_hz, self._band)
        else:
            await self.start(freq_hz, self._band)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self, freq_hz: float, band: str, gain):
        from rtlsdr import RtlSdr
        sdr = RtlSdr()
        self._sdr = sdr

        sr = _FM_SR if band in ("fm", "hd") else _AM_SR
        block = _BLOCK if band in ("fm", "hd") else _AM_BLOCK

        sdr.sample_rate = sr
        sdr.center_freq = freq_hz

        if band == "am":
            sdr.set_direct_sampling("q")
        else:
            sdr.set_direct_sampling(0)

        if gain == "auto":
            sdr.gain = "auto"
        else:
            sdr.gain = float(gain)

        logger.info("SDR started: %.3f MHz [%s] at %.0f MHz SR", freq_hz / 1e6, band, sr / 1e6)

        loop = asyncio.get_event_loop()

        from ..streaming import AacEncoder
        encoder = AacEncoder(stereo=(band in ("fm", "hd")))

        first_chunk = True
        try:
            async for iq in sdr.stream(block):
                chunk = await loop.run_in_executor(
                    self._executor,
                    self._process, iq, encoder,
                )
                if chunk:
                    self._streams.broadcast(chunk)
                    if first_chunk:
                        first_chunk = False
                        self._meta.update_state("live")
                        asyncio.ensure_future(self._meta.broadcast())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("SDR stream error: %s", e)
        finally:
            try:
                await sdr.stop()
            except Exception:
                pass
            try:
                sdr.close()
            except Exception:
                pass
            self._sdr = None
            encoder.close()
            logger.info("SDR stopped")

    def _process(self, iq: np.ndarray, encoder) -> Optional[bytes]:
        """Runs in executor thread: demodulate → encode → return AAC bytes."""
        try:
            if self._band == "fm":
                l, r, composite = self._demod.process(iq)
                if self._rds is not None:
                    # Generate pilot analytic for RDS carrier reference
                    pilot_a = hilbert(composite).astype(np.complex64)
                    self._rds.feed(composite, pilot_a)
                return encoder.encode(l, r)

            elif self._band in ("am", "scanner"):
                mono = self._demod.process(iq)
                return encoder.encode(mono)

        except Exception as e:
            logger.debug("DSP error: %s", e)
        return None

    @property
    def signal_quality(self) -> float:
        """
        0.0–1.0 signal quality estimate.
        FM: pilot RMS (proxy for SNR). AM/scanner: 0.3 when running.
        """
        if self._band == "fm" and self._demod is not None:
            return float(getattr(self._demod, "last_pilot_rms", 0.0))
        if self._demod is not None:
            return 0.3
        return 0.0

    def _make_demod(self, band: str, freq_hz: float, deemphasis_us: int):
        if band == "fm":
            return FmStereoDemodulator(deemphasis_us=deemphasis_us)
        elif band == "scanner" and _AVIATION_LO <= freq_hz <= _AVIATION_HI:
            return AmDemodulator()
        elif band == "scanner":
            return NfmDemodulator()
        elif band == "am":
            return AmDemodulator()
        return None

    def _on_rds(self, data: dict):
        """Called from executor thread when RDS fields are decoded."""
        # schedule the metadata update on the event loop
        asyncio.get_event_loop().call_soon_threadsafe(
            self._meta.update_rds,
            data.get("ps") or None,
            data.get("rt") or None,
            data.get("pty") or None,
            data.get("pi") or None,
        )
