"""
RadioPipeline — owns the RTL-SDR device, DSP, and feeds encoded AAC
chunks to the StreamingManager.

FM: 1.2 MHz sample rate, stereo (custom demod + RDS + HD detection)
AM / Scanner / WX: 1.2 MHz, mono; AM uses direct sampling, WX and the
non-aviation scanner bands use narrowband FM.

Idle behaviour: while no listener/recorder is connected (and Icecast
keep_alive isn't holding the stream open), the SDR device is fully
closed — no tuner power, no 2.4 MB/s USB DMA — and reopened on the next
client.  The dongle is usually the hottest component in the enclosure,
so this matters more for average temperature than any DSP optimisation.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np

from .fm import FmStereoDemodulator
from .am import AmDemodulator, NfmDemodulator
from .hd_detect import HdSidebandDetector
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
        await pipeline.retune(new_freq)   # same-band FM: no SDR restart
        await pipeline.close()            # terminal: releases executors
    """

    def __init__(self, config: dict, metadata, streaming_manager):
        self._cfg      = config
        self._meta     = metadata
        # Pipelines are created after update_tune() bumps the generation;
        # late RDS callbacks from this pipeline after a retune carry this
        # value and get dropped by MetadataState.
        self._meta_gen = getattr(metadata, "tune_generation", 0)
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
        self._gain = "auto"
        self._deemphasis_us = 75
        self._stereo_mode = "auto"
        self._demod = None
        self._rds:  Optional[RdsDecoder] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._squelch_iq: float = 0.0      # 0 = disabled; mute audio when iq_rms < threshold
        self._squelch_silence_n: int = 5243  # output samples per block; updated on first live block
        self._current_gain: Optional[float] = None   # dB; None when hardware auto
        # Software gain controller state — persists across idle SDR
        # sessions so gain doesn't restart from scratch on every listener
        # reconnect to the same station.
        self._avail_gains: list = []
        self._gain_idx: int = 0
        self._hd_detect: Optional[HdSidebandDetector] = None   # FM band only
        self._hd_detect_countdown = 0
        # True until the next encoded chunk should flip state to "live"
        # (set on start and retune; consumed in the session loop).
        self._announce_live = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, freq_hz: float, band: str, gain="auto", deemphasis_us: int = 75, stereo_mode: str = "auto"):
        await self.stop()
        self._band = band
        self._freq = freq_hz
        self._gain = gain
        self._deemphasis_us = deemphasis_us
        self._stereo_mode = stereo_mode
        self._announce_live = True
        self._demod = self._make_demod(band, freq_hz, deemphasis_us, stereo_mode)

        if band == "fm":
            self._rds = RdsDecoder(self._on_rds)
            self._hd_detect = HdSidebandDetector()
        else:
            self._rds = None
            self._hd_detect = None
        self._hd_detect_countdown = 0

        self._task = asyncio.create_task(
            self._run(band, gain),
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

    async def close(self):
        """Terminal shutdown: stop streaming and release the executor
        threads.  A stopped pipeline can be start()ed again; a closed one
        cannot.  RadioManager creates a fresh pipeline per full tune —
        without this, the three executors leaked four threads per tune."""
        await self.stop()
        for ex in (self._executor, self._ss_executor, self._rds_executor):
            ex.shutdown(wait=False, cancel_futures=True)

    def set_squelch(self, threshold: float):
        """IQ RMS below which audio is muted; 0 disables squelch."""
        self._squelch_iq = max(0.0, float(threshold))

    async def retune(self, freq_hz: float):
        """Change frequency within the same band without restarting the SDR.

        FM only.  The demodulator, RDS decoder, and HD detector carry the
        old station's state (AGC level, blend, MinStat noise history, bit
        sync), so fresh instances are created; the SDR device, session,
        and encoder keep running, so audio continues without a reconnect
        gap.  If the SDR is idle-closed, only the frequency is recorded —
        the next session opens on it.
        """
        if self._band != "fm" or self._task is None or self._task.done():
            await self.start(freq_hz, self._band or "fm", self._gain,
                             self._deemphasis_us, self._stereo_mode)
            return

        self._freq = freq_hz
        self._meta.update_tune(freq_hz, self._band)
        # update_tune bumped the tune generation — refresh ours, or every
        # subsequent RDS callback would be dropped as stale.
        self._meta_gen = self._meta.tune_generation
        self._demod = self._make_demod("fm", freq_hz, self._deemphasis_us, self._stereo_mode)
        self._rds = RdsDecoder(self._on_rds)
        self._hd_detect = HdSidebandDetector()
        self._hd_detect_countdown = 0
        self._announce_live = True

        sdr = self._sdr
        if sdr is not None:
            try:
                sdr.center_freq = freq_hz
            except Exception as e:
                logger.warning("Retune failed, restarting: %s", e)
                await self.start(freq_hz, self._band, self._gain,
                                 self._deemphasis_us, self._stereo_mode)
                return
        logger.info("Retuned to %.3f MHz [%s]", freq_hz / 1e6, self._band)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self, band: str, gain):
        from ..streaming import AacEncoder
        encoder = AacEncoder(stereo=(band in ("fm", "hd")))
        loop = asyncio.get_event_loop()
        self._loop = loop   # saved for use in the executor-thread _on_rds callback
        try:
            while True:
                # SDR stays fully closed until someone needs audio: browser
                # listeners and the recorder register as real clients, and
                # Icecast keep_alive mode does too, holding the event set.
                await self._streams.wait_for_clients()
                await self._sdr_session(band, gain, encoder, loop)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("SDR stream error: %s", e)
            # Push an error state so clients don't show "Buffering…" forever
            self._meta.update_state("error")
            await self._meta.broadcast()
        finally:
            encoder.close()
            logger.info("SDR pipeline stopped")

    async def _sdr_session(self, band: str, gain, encoder, loop):
        """One SDR power-on: open the device, stream until every client
        leaves (after StreamingManager's grace period), then close it."""
        from rtlsdr import RtlSdr
        sdr = RtlSdr()
        self._sdr = sdr

        sr = _FM_SR if band in ("fm", "hd") else _AM_SR
        block = _BLOCK if band in ("fm", "hd") else _AM_BLOCK

        sdr.sample_rate = sr
        # Apply the dongle's crystal correction BEFORE tuning.  This was
        # never wired up: harmless on wideband FM (a few kHz against 75 kHz
        # deviation) but fatal on NFM/WX, where the same offset pushes the
        # signal outside the ±10 kHz channel filter.
        ppm = int((self._cfg.get("sdr") or {}).get("ppm_correction", 0) or 0)
        if ppm:
            try:
                sdr.freq_correction = ppm
            except Exception as e:
                logger.warning("ppm correction (%d) failed: %s", ppm, e)
        sdr.center_freq = self._freq

        if band == "am":
            sdr.set_direct_sampling("q")
        else:
            sdr.set_direct_sampling(0)

        # For FM/WX with gain="auto", replace the hardware AGC with software
        # gain control.  The R820T2 hardware AGC targets ADC headroom only
        # and typically lands at 40-49 dB, which degrades FM SNR compared
        # to the ~30 dB noise-figure optimum.  We start near 30 dB and
        # step only to keep the IQ RMS inside the safe operating range.
        software_gain = band in ("fm", "wx") and gain == "auto"
        if software_gain:
            if not self._avail_gains:
                # rtlsdr_get_tuner_gains() returns tenths-of-dB (297 = 29.7 dB).
                self._avail_gains = sorted(v / 10.0 for v in sdr.gain_values)
                self._gain_idx = min(
                    range(len(self._avail_gains)),
                    key=lambda i: abs(self._avail_gains[i] - _FM_GAIN_START))
            sdr.gain = self._avail_gains[self._gain_idx]
            self._current_gain = self._avail_gains[self._gain_idx]
            logger.info("Software gain control: starting at %.1f dB", self._current_gain)
        else:
            if gain == "auto":
                sdr.gain = "auto"
                self._current_gain = None
            else:
                sdr.gain = float(gain)
                self._current_gain = float(gain)

        logger.info("SDR session started: %.3f MHz [%s] at %.1f MHz SR",
                    self._freq / 1e6, band, sr / 1e6)

        hold_blocks = 0   # blocks since last gain step
        try:
            async for iq in sdr.stream(block):
                if not self._streams.is_active():
                    logger.info("No audio clients — closing SDR (tuner off)")
                    break
                chunk = await loop.run_in_executor(
                    self._executor,
                    self._process, iq, encoder,
                )
                if chunk:
                    self._streams.broadcast(chunk)
                    if self._announce_live:
                        self._announce_live = False
                        self._meta.update_state("live")
                        asyncio.ensure_future(self._meta.broadcast())

                # Software gain control step (FM/WX only)
                if software_gain and self._demod is not None:
                    hold_blocks += 1
                    if hold_blocks >= _GAIN_HOLD_BLOCKS:
                        hold_blocks = 0
                        self._gain_step(sdr)
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
            logger.info("SDR session closed")

    def _gain_step(self, sdr):
        """One software gain-controller decision (called every ~5 s)."""
        avail = self._avail_gains
        g_idx = self._gain_idx
        iq_rms      = self._demod.last_iq_rms
        pilot_rms   = getattr(self._demod, "last_pilot_rms", 0.0)
        noise_rms   = getattr(self._demod, "last_noise_rms",  0.0)
        noise_ratio = noise_rms / (pilot_rms + 1e-6)

        if iq_rms > _IQ_RMS_HI and g_idx > 0:
            # ADC saturation risk
            g_idx -= 1
            reason = f"iq_rms={iq_rms:.3f}, ADC headroom"
        elif noise_ratio > _NOISE_RATIO_MAX and iq_rms > _IQ_RMS_LO and g_idx > 0:
            # More gain is worsening FM SNR (pre-amp overshoot or
            # chip noise figure degrading above optimum)
            g_idx -= 1
            reason = f"noise_ratio={noise_ratio:.2f}, quality"
        elif iq_rms < _IQ_RMS_LO and g_idx < len(avail) - 1:
            # Signal too weak
            g_idx += 1
            reason = f"iq_rms={iq_rms:.3f}, weak signal"
        else:
            return

        direction = "↑" if g_idx > self._gain_idx else "↓"
        self._gain_idx = g_idx
        sdr.gain = avail[g_idx]
        self._current_gain = avail[g_idx]
        logger.info("Gain %s %.1f dB (%s)", direction, avail[g_idx], reason)

    def _process(self, iq: np.ndarray, encoder) -> Optional[bytes]:
        """Runs in executor thread: demodulate → encode → return AAC bytes."""
        try:
            # Local refs: retune() swaps these from the event loop while a
            # block may be in flight here — one whole block on the old
            # station is fine, a mid-block mix is not.
            demod = self._demod
            if demod is None:
                return None
            iq_rms = float(np.sqrt(np.mean(iq.real**2 + iq.imag**2)))
            demod.last_iq_rms = iq_rms

            if self._band == "fm":
                # HD sideband sniff on the raw IQ (FM only) — every ~10 blocks
                # (~2 s); the sidebands live at ±135-195 kHz, which the audio
                # decimation below throws away.
                if self._hd_detect is not None:
                    self._hd_detect_countdown -= 1
                    if self._hd_detect_countdown <= 0:
                        self._hd_detect_countdown = 10
                        self._hd_detect.process(iq)
                if self._squelch_iq > 0 and iq_rms < self._squelch_iq:
                    n = self._squelch_silence_n
                    return encoder.encode(np.zeros(n, np.float32), np.zeros(n, np.float32))
                l, r, composite = demod.process(iq)
                if len(l) == 0:
                    return None
                self._squelch_silence_n = len(l)
                if self._rds is not None:
                    # Fire-and-forget: submit RDS to its own thread so AAC
                    # encoding can start immediately.  composite.copy() is
                    # required — the FM demod and RDS thread must not share
                    # the same array.  Errors are logged inside _rds_feed.
                    self._rds_executor.submit(self._rds_feed, composite.copy())
                return encoder.encode(l, r)

            elif self._band in ("am", "scanner", "wx"):
                if self._squelch_iq > 0 and iq_rms < self._squelch_iq:
                    n = self._squelch_silence_n
                    return encoder.encode(np.zeros(n, np.float32))
                mono = demod.process(iq)
                if len(mono) == 0:
                    return None
                self._squelch_silence_n = len(mono)
                return encoder.encode(mono)

        except Exception as e:
            logger.warning("DSP error: %s", e)
        return None

    @property
    def hd_available(self) -> bool:
        """True when IBOC digital sidebands are detected on the tuned FM station."""
        return self._hd_detect is not None and self._hd_detect.available

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
        if self._band == "fm":
            d["composite_rms"] = float(getattr(self._demod, "last_composite_rms", 0.0))
            d["pilot_rms"]     = float(getattr(self._demod, "last_pilot_rms",     0.0))
            d["noise_rms"]     = float(getattr(self._demod, "last_noise_rms",     0.0))
            d["blend"]         = float(getattr(self._demod, "last_blend",         0.0))
            d["audio_rms"]     = float(getattr(self._demod, "last_audio_rms",     0.0))
            # ppm calibration aid: crystal ppm ≈ −pilot_offset_hz / 0.019
            d["pilot_offset_hz"] = round(
                float(getattr(self._demod, "last_pilot_offset_hz", 0.0)), 3)
        if self._hd_detect is not None:
            d["hd_ratio"] = round(self._hd_detect.ratio, 3)   # calibration aid
        if self._current_gain is not None:
            d["gain_db"] = self._current_gain
        return d

    def _make_demod(self, band: str, freq_hz: float, deemphasis_us: int, stereo_mode: str = "auto"):
        if band == "fm":
            return FmStereoDemodulator(deemphasis_us=deemphasis_us, stereo_mode=stereo_mode,
                                       ss_executor=self._ss_executor)
        elif band == "wx":
            # NOAA weather radio is narrowband FM (5 kHz deviation), not
            # broadcast WFM — the stereo demod expected 75 kHz deviation
            # and produced quiet audio with 10 kHz of pure noise on top.
            return NfmDemodulator()
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
            data.get("rt_partial", False),
            self._meta_gen,          # dropped by MetadataState if a retune happened
        )
