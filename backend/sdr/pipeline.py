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

from .fm import FmStereoDemodulator
from .am import AmDemodulator, NfmDemodulator
from .rds import RdsDecoder

logger = logging.getLogger(__name__)

_FM_SR   = 1_200_000   # 2.4 MHz caused queue overflow; 1.2 MHz is sufficient for FM stereo + RDS
_AM_SR   = 1_200_000
_BLOCK    = 262_144     # IQ samples per SDR read (≈ 218 ms at 1.2 MHz); doubled from 131072 to
                        # halve per-block Python overhead with no audio quality impact
_AM_BLOCK = 131_072     # IQ samples per read at 1.2 MHz (≈ 109 ms)

# RTL-SDR / R820T2 software gain control for FM.
# Hardware "auto" AGC targets ADC headroom, not FM SNR.  Above ~35 dB the
# LNA noise figure degrades faster than the gain helps (especially when a
# pre-amp is present and already provides sufficient low-noise amplification).
# Two criteria drive the controller:
#   Level-based: keep IQ RMS in [_IQ_RMS_LO, _IQ_RMS_HI]
#   Quality-based: step DOWN if noise_rms/pilot_rms > _NOISE_RATIO_MAX
#                  (more gain is worsening FM SNR, not improving it)
_FM_GAIN_START    = 30.0   # dB — starting point; closest available gain used
_IQ_RMS_LO        = 0.07   # below this: signal too weak, step up (was 0.10 — too
                            # aggressive; caused overshoot past SNR optimum)
_IQ_RMS_HI        = 0.38   # above this: ADC saturation risk, step down
_NOISE_RATIO_MAX  = 2.0    # noise_rms/pilot_rms above this: step down for quality
_GAIN_HOLD_BLOCKS = 25     # minimum blocks between gain steps (~5 s at 218 ms/block)

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
        self._executor     = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sdr-dsp")
        # Dedicated 2-worker pool for parallel L/R spectral subtraction.
        # Both channels are independent so they can run concurrently; NumPy
        # FFT operations release the GIL, allowing genuine multi-core use.
        self._ss_executor  = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sdr-ss")
        # Fire-and-forget RDS executor — decouples RDS decoding from the
        # DSP thread so AAC encoding proceeds immediately after FM demod.
        # RDS metadata changes on ~1 s timescales; occasional dropped blocks
        # (if the RDS thread lags) have no perceptible effect.
        self._rds_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sdr-rds")
        self._band: Optional[str] = None
        self._freq: Optional[float] = None
        self._demod = None
        self._rds:  Optional[RdsDecoder] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._squelch_iq: float = 0.0      # 0 = disabled; mute audio when iq_rms < threshold
        self._squelch_silence_n: int = 5243  # output samples per block; updated on first live block
        self._current_gain: Optional[float] = None   # dB; None when hardware auto

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, freq_hz: float, band: str, gain="auto", deemphasis_us: int = 75, stereo_mode: str = "auto"):
        await self.stop()
        self._band = band
        self._freq = freq_hz
        self._demod = self._make_demod(band, freq_hz, deemphasis_us, stereo_mode)

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

        sr = _FM_SR if band in ("fm", "hd", "wx") else _AM_SR
        block = _BLOCK if band in ("fm", "hd", "wx") else _AM_BLOCK

        sdr.sample_rate = sr
        sdr.center_freq = freq_hz

        if band == "am":
            sdr.set_direct_sampling("q")
        else:
            sdr.set_direct_sampling(0)

        # For FM with gain="auto", replace the hardware AGC with software
        # gain control.  The R820T2 hardware AGC targets ADC headroom only
        # and typically lands at 40-49 dB, which degrades FM SNR compared
        # to the ~30 dB noise-figure optimum.  We start near 30 dB and
        # step only to keep the IQ RMS inside the safe operating range.
        if band in ("fm", "wx") and gain == "auto":
            # rtlsdr_get_tuner_gains() returns tenths-of-dB (e.g. 297 = 29.7 dB).
            # pyrtlsdr exposes these raw values via gain_values; divide by 10 so
            # our search and sdr.gain setter (which expects dB) both work correctly.
            avail_gains = sorted(v / 10.0 for v in sdr.gain_values)
            g_idx = min(range(len(avail_gains)),
                        key=lambda i: abs(avail_gains[i] - _FM_GAIN_START))
            sdr.gain = avail_gains[g_idx]
            self._current_gain = avail_gains[g_idx]
            logger.info("FM software gain control: starting at %.1f dB", avail_gains[g_idx])
        else:
            avail_gains = []
            g_idx = 0
            if gain == "auto":
                sdr.gain = "auto"
                self._current_gain = None
            else:
                sdr.gain = float(gain)
                self._current_gain = float(gain)

        logger.info("SDR started: %.3f MHz [%s] at %.0f MHz SR", freq_hz / 1e6, band, sr / 1e6)

        loop = asyncio.get_event_loop()
        self._loop = loop   # saved for use in the executor-thread _on_rds callback

        from ..streaming import AacEncoder
        encoder = AacEncoder(stereo=(band in ("fm", "hd", "wx")))

        first_chunk = True
        hold_blocks = 0   # blocks since last gain step
        try:
            async for iq in sdr.stream(block):
                # Skip DSP when no clients are active, but keep consuming IQ
                # blocks so the rtlsdraio USB queue never backs up.
                if not self._streams.is_active():
                    continue
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

                # Software gain control step (FM only)
                if avail_gains and self._demod is not None:
                    hold_blocks += 1
                    if hold_blocks >= _GAIN_HOLD_BLOCKS:
                        hold_blocks = 0
                        iq_rms      = self._demod.last_iq_rms
                        pilot_rms   = getattr(self._demod, "last_pilot_rms", 0.0)
                        noise_rms   = getattr(self._demod, "last_noise_rms",  0.0)
                        noise_ratio = noise_rms / (pilot_rms + 1e-6)

                        if iq_rms > _IQ_RMS_HI and g_idx > 0:
                            # ADC saturation risk
                            g_idx -= 1
                            sdr.gain = avail_gains[g_idx]
                            self._current_gain = avail_gains[g_idx]
                            logger.info("FM gain ↓ %.1f dB (iq_rms=%.3f, ADC headroom)",
                                        avail_gains[g_idx], iq_rms)
                        elif noise_ratio > _NOISE_RATIO_MAX and iq_rms > _IQ_RMS_LO and g_idx > 0:
                            # More gain is worsening FM SNR (pre-amp overshoot or
                            # chip noise figure degrading above optimum)
                            g_idx -= 1
                            sdr.gain = avail_gains[g_idx]
                            self._current_gain = avail_gains[g_idx]
                            logger.info("FM gain ↓ %.1f dB (noise_ratio=%.2f, quality)",
                                        avail_gains[g_idx], noise_ratio)
                        elif iq_rms < _IQ_RMS_LO and g_idx < len(avail_gains) - 1:
                            # Signal too weak
                            g_idx += 1
                            sdr.gain = avail_gains[g_idx]
                            self._current_gain = avail_gains[g_idx]
                            logger.info("FM gain ↑ %.1f dB (iq_rms=%.3f, weak signal)",
                                        avail_gains[g_idx], iq_rms)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("SDR stream error: %s", e)
            # Push an error state so clients don't show "Buffering…" forever
            self._meta.update_state("error")
            await self._meta.broadcast()
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
            if self._band in ("fm", "wx"):
                # Cheap IQ RMS check before the full DSP chain.  When squelched
                # we skip everything (resample, filters, Hilbert, spectral sub,
                # AGC) and return silence directly.  last_iq_rms is updated so
                # the software gain controller keeps working.
                iq_rms = float(np.sqrt(np.mean(iq.real**2 + iq.imag**2)))
                if self._demod is not None:
                    self._demod.last_iq_rms = iq_rms
                if self._squelch_iq > 0 and iq_rms < self._squelch_iq:
                    n = self._squelch_silence_n
                    return encoder.encode(np.zeros(n, np.float32), np.zeros(n, np.float32))
                l, r, composite = self._demod.process(iq)
                self._squelch_silence_n = len(l)
                if self._rds is not None:
                    # Fire-and-forget: submit RDS to its own thread so AAC
                    # encoding can start immediately.  composite.copy() is
                    # required — the FM demod and RDS thread must not share
                    # the same array.  Errors are logged inside _rds_feed.
                    self._rds_executor.submit(self._rds_feed, composite.copy())
                return encoder.encode(l, r)

            elif self._band in ("am", "scanner"):
                iq_rms = float(np.sqrt(np.mean(iq.real**2 + iq.imag**2)))
                if self._demod is not None:
                    self._demod.last_iq_rms = iq_rms
                if self._squelch_iq > 0 and iq_rms < self._squelch_iq:
                    n = self._squelch_silence_n
                    return encoder.encode(np.zeros(n, np.float32))
                mono = self._demod.process(iq)
                self._squelch_silence_n = len(mono)
                return encoder.encode(mono)

        except Exception as e:
            logger.warning("DSP error: %s", e)
        return None

    @property
    def signal_strength(self) -> float:
        """IQ RMS of the last received block — direct measure of RF/antenna signal level."""
        if self._demod is not None:
            return float(getattr(self._demod, "last_iq_rms", 0.0))
        return 0.0

    @property
    def signal_quality(self) -> float:
        """Pilot RMS for FM (used for stereo detection); 0.3 when running for AM/scanner."""
        if self._band == "fm" and self._demod is not None:
            return float(getattr(self._demod, "last_pilot_rms", 0.0))
        if self._demod is not None:
            return 0.3
        return 0.0

    def get_diag(self) -> dict:
        """Snapshot of current DSP diagnostic values for the WebSocket broadcast."""
        if not self._demod:
            return {}
        d: dict = {
            "iq_rms": float(getattr(self._demod, "last_iq_rms", 0.0)),
        }
        if self._band in ("fm", "wx"):
            d["composite_rms"] = float(getattr(self._demod, "last_composite_rms", 0.0))
            d["pilot_rms"]     = float(getattr(self._demod, "last_pilot_rms",     0.0))
            d["noise_rms"]     = float(getattr(self._demod, "last_noise_rms",     0.0))
            d["blend"]         = float(getattr(self._demod, "last_blend",         0.0))
            d["audio_rms"]     = float(getattr(self._demod, "last_audio_rms",     0.0))
        if self._current_gain is not None:
            d["gain_db"] = self._current_gain
        return d

    def _make_demod(self, band: str, freq_hz: float, deemphasis_us: int, stereo_mode: str = "auto"):
        if band in ("fm", "wx"):
            return FmStereoDemodulator(deemphasis_us=deemphasis_us, stereo_mode=stereo_mode,
                                       ss_executor=self._ss_executor)
        elif band == "scanner" and _AVIATION_LO <= freq_hz <= _AVIATION_HI:
            return AmDemodulator()
        elif band == "scanner":
            return NfmDemodulator()
        elif band == "am":
            return AmDemodulator()
        return None

    def _rds_feed(self, composite: np.ndarray):
        """Runs in _rds_executor thread — feeds composite to the RDS decoder."""
        rds = self._rds
        if rds is None:
            return
        try:
            rds.feed(composite)
        except Exception as e:
            logger.warning("RDS error: %s", e)

    def _on_rds(self, data: dict):
        """Called from the executor thread — must not use asyncio directly."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        # call_soon_threadsafe is the correct way to schedule from a thread
        loop.call_soon_threadsafe(
            self._meta.update_rds,
            data.get("ps") or None,
            data.get("rt") or None,
            data.get("pty") or None,
            data.get("pi") or None,
            data.get("rtp_title"),   # RT+ structured title (None if not received)
            data.get("rtp_artist"),  # RT+ structured artist (None if not received)
        )
