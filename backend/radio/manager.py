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
import math
from typing import Optional

import numpy as np

from ..metadata import MetadataState
from ..streaming import StreamingManager, AacEncoder
from .nrsc5_backend import Nrsc5Backend

logger = logging.getLogger(__name__)

# Aviation band uses AM demodulation
_AVIATION_LO = 118e6
_AVIATION_HI = 137e6

# nrsc5 outputs 44100 Hz PCM in ~1024-sample blocks (~23 ms).  We need 48000 Hz.
# resample_poly(160, 147) uses a 3201-tap FIR (half-length 1600 samples) — longer
# than the entire block, so every output sample falls within both edge artifact zones.
# Context-carry attempts moved the dominant artifact to the right edge, which locked
# it to a consistent phase (~0.44) within every block, manifesting as a periodic
# click pattern at 43 Hz.  Linear interpolation (np.interp) is stateless, has zero
# block-edge artifacts, and introduces only a mild sinc rolloff (≤ −1.5 dB at 14 kHz,
# ≤ −2.5 dB at 18 kHz) — well within the tolerance of the broadcast chain.


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
        # DSP params of the running pipeline — a new tune with identical
        # params on the same FM band takes the fast retune path.
        self._last_params: Optional[tuple] = None
        self._tune_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def startup(self):
        pass  # nothing to pre-create

    async def tune(self, freq_hz: float, band: str, **kwargs):
        # Serialize tunes — rapid successive requests must not interleave
        # _stop_all() with a concurrent pipeline start.
        async with self._tune_lock:
            await self._tune_locked(freq_hz, band, **kwargs)

    async def _tune_locked(self, freq_hz: float, band: str, **kwargs):
        gain         = kwargs.get("gain", self._cfg.get("sdr", {}).get("gain", "auto"))
        deemph       = kwargs.get("deemphasis_us", self._deemphasis)
        stereo_mode  = kwargs.get("stereo_mode", "auto")
        # hd_channel is 1-based from the frontend; convert to 0-based for nrsc5
        hd_channel_1 = int(kwargs.get("hd_channel", 1))
        hd_program   = max(0, hd_channel_1 - 1)

        # Fast path: FM → FM frequency change with identical DSP params —
        # swap decoders and hop the tuner inside the running pipeline.  The
        # SDR session, encoder, and client connections stay up, so dial
        # steps don't tear down and rebuild the whole stack (which also
        # leaked executor threads per tune).
        if (band == "fm" and self._current_band == "fm"
                and self._pipeline is not None
                and (gain, deemph, stereo_mode) == self._last_params):
            self._streams.drain_all()
            self._current_freq = freq_hz
            await self._pipeline.retune(freq_hz)   # does update_tune itself
            self._meta.update_state("buffering")
            await self._meta.broadcast()
            if not self._signal_task or self._signal_task.done():
                self._signal_task = asyncio.create_task(self._signal_loop())
            logger.info("Fast retune to %.3f MHz [fm]", freq_hz / 1e6)
            return

        self._meta.update_tune(freq_hz, band)
        await self._meta.broadcast()

        await self._stop_all()
        self._streams.drain_all()

        self._current_freq = freq_hz
        self._current_band = band
        self._last_params  = (gain, deemph, stereo_mode)

        if band == "hd":
            await self._start_hd(freq_hz, program=hd_program)
        else:
            await self._start_pipeline(freq_hz, band, gain, deemph, stereo_mode)

        self._meta.update_state("buffering")
        await self._meta.broadcast()

        if not self._signal_task or self._signal_task.done():
            self._signal_task = asyncio.create_task(self._signal_loop())

        logger.info("Tuned to %.3f MHz [%s]", freq_hz / 1e6, band)

    def set_squelch(self, slider: int):
        """Map UI slider (0-100) to an IQ RMS threshold. 0 = disabled."""
        threshold = slider * 0.002 if slider > 0 else 0.0
        if self._pipeline:
            self._pipeline.set_squelch(threshold)
        logger.info("Squelch threshold: %.3f (slider=%d)", threshold, slider)

    async def stop(self):
        async with self._tune_lock:
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

    async def _start_pipeline(self, freq_hz: float, band: str, gain, deemph: int, stereo_mode: str = "auto"):
        from ..sdr.pipeline import RadioPipeline
        self._pipeline = RadioPipeline(self._cfg, self._meta, self._streams)
        await self._pipeline.start(freq_hz, band, gain=gain, deemphasis_us=deemph, stereo_mode=stereo_mode)

    async def _start_hd(self, freq_hz: float, program: int = 0):
        # Record which channel we're on (1-based) before nrsc5 starts producing output
        self._meta.hd_channel = program + 1
        encoder = AacEncoder(stereo=True)
        _live       = [False]   # mutable flag for the pcm_cb closure
        _in_total   = [0]       # total input samples consumed — keeps output grid continuous

        gen = self._meta.tune_generation   # captured after update_tune bumped it

        def meta_cb(data: dict):
            self._meta.update_nrsc5(gen=gen, **data)

        def pcm_cb(pcm_l: object, pcm_r: object):
            # Transition to "live" on the first audio chunk so the frontend
            # stops showing "Buffering…" and displays station metadata instead.
            if not _live[0]:
                _live[0] = True
                self._meta.update_state("live")
                asyncio.ensure_future(self._meta.broadcast())

            l_in = np.asarray(pcm_l, np.float32)
            r_in = np.asarray(pcm_r, np.float32)
            n_in = len(l_in)

            # Click blanker — interpolate over nrsc5 glitch bursts before resampling.
            # Threshold matches the FM soft-limiter knee (0.85); legitimate HD audio
            # rarely reaches this level.
            for sig in (l_in, r_in):
                ck        = np.abs(sig) > 0.85
                ck[1:]   |= ck[:-1]
                ck[:-1]  |= ck[1:]
                if ck.any():
                    xi = np.where(~ck)[0]
                    if len(xi) >= 2:
                        sig[ck] = np.interp(np.where(ck)[0], xi, sig[xi])

            # Resample 44100 → 48000 Hz with stateful linear interpolation.
            #
            # Simple per-block np.interp(arange(out_len) * 147/160, ...) restarts
            # t_out at 0 each call, leaving a compressed inter-block gap (step 0.51
            # vs normal 0.919 input samples) that creates 43 Hz phase-modulation
            # sidebands at −40 dB.  The stateful counter (_in_total) makes the output
            # index grid continuous across calls — sidebands drop to below −100 dB.
            # At most one output sample per block uses constant-edge extrapolation
            # (np.interp clamp) by ≤ 0.41 samples — negligible.
            start_in          = _in_total[0]
            _in_total[0]     += n_in
            out_start         = math.ceil(start_in * 160 / 147)
            out_end           = math.ceil(_in_total[0] * 160 / 147)
            t_out             = np.arange(out_start, out_end) * (147.0 / 160.0) - start_in
            t_in              = np.arange(n_in, dtype=np.float64)
            l = np.interp(t_out, t_in, l_in).astype(np.float32)
            r = np.interp(t_out, t_in, r_in).astype(np.float32)

            chunk = encoder.encode(l, r)
            if chunk:
                self._streams.broadcast(chunk)

        self._nrsc5 = Nrsc5Backend(
            device_index=self._device_index,
            metadata_callback=meta_cb,
            pcm_callback=pcm_cb,
        )
        await self._nrsc5.start(freq_hz, program=program)

    async def _stop_all(self):
        if self._pipeline:
            # close() (not stop()) — releases the pipeline's executor
            # threads; a new pipeline is created per full tune.
            await self._pipeline.close()
            self._pipeline = None
            self._last_params = None
        if self._nrsc5:
            await self._nrsc5.stop()
            self._nrsc5 = None

    # ------------------------------------------------------------------
    # Signal loop — broadcasts metadata + signal bars every second
    # ------------------------------------------------------------------

    async def _signal_loop(self):
        _prev = (-1, None, None)   # (bars, stereo, state) — track last broadcast
        try:
            while True:
                await asyncio.sleep(1)
                bars, stereo = self._estimate_signal()
                self._meta.update_signal(bars, stereo)

                # Collect live DSP diagnostics from the pipeline (FM/WX only).
                # Always broadcast when diagnostics are present so the meters
                # update at 1 Hz even when signal bars and state are steady.
                has_diag = False
                if self._pipeline:
                    diag = self._pipeline.get_diag()
                    if diag:
                        self._meta.diag = diag
                        has_diag = True

                hd_avail = bool(self._pipeline is not None
                                and getattr(self._pipeline, "hd_available", False))
                self._meta.hd_available = hd_avail

                cur = (bars, stereo, self._meta.state, hd_avail)
                if cur != _prev or has_diag:
                    _prev = cur
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
