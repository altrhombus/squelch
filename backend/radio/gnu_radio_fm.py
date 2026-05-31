"""
GNU Radio FM stereo demodulator with inline RDS decoding.

Requires: gnuradio, gr-osmosdr, gr-rds
  sudo apt-get install gnuradio gr-osmosdr gr-rds

Audio pipeline:  osmosdr.source → wbfm_receive → file_sink(FIFO)
RDS pipeline:    FM demod → rds.decoder → rds.parser → Python message callback
"""

import logging
import os
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class GnuRadioFM:
    """
    Runs a GNU Radio FM stereo + RDS flowgraph in a background thread.
    Writes interleaved stereo s16le PCM at 50kHz to `fifo_path`.
    Calls `rds_callback({ps, rt, pty, pi})` when RDS data arrives.
    """

    SAMPLE_RATE = 2_000_000   # SDR capture rate
    DEMOD_RATE  = 200_000     # After channel filter decimation (2MHz / 10)
    AUDIO_DECIM = 4           # wfm_rcv_pll audio decimation: 200kHz / 4 = 50kHz
    AUDIO_RATE  = 50_000      # Output PCM rate

    def __init__(
        self,
        fifo_path: str,
        device_index: int = 0,
        ppm_correction: int = 0,
        deemphasis_us: int = 75,
        rds_callback: Optional[Callable[[dict], None]] = None,
    ):
        self._fifo_path = fifo_path
        self._device_index = device_index
        self._ppm_correction = ppm_correction
        self._deemphasis_us = deemphasis_us
        self._rds_callback = rds_callback
        self._tb = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, freq_hz: float, gain="auto", bandwidth="wide"):
        self._stop_flowgraph()
        self._freq_hz = freq_hz
        self._gain = gain
        self._bandwidth = bandwidth
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def tune(self, freq_hz: float):
        """Retune without rebuilding the flowgraph — avoids FIFO gap."""
        self._freq_hz = freq_hz
        if self._tb:
            try:
                self._tb.src.set_center_freq(freq_hz)
                logger.info("Retuned to %.3f MHz", freq_hz / 1e6)
            except Exception as e:
                logger.warning("Retune failed, restarting: %s", e)
                self.start(freq_hz)

    def set_gain(self, gain):
        self._gain = gain
        if self._tb:
            try:
                if gain == "auto":
                    self._tb.src.set_gain_mode(True, 0)
                else:
                    self._tb.src.set_gain_mode(False, 0)
                    self._tb.src.set_gain(float(gain), 0)
            except Exception as e:
                logger.warning("Failed to set gain: %s", e)

    def set_stereo_mode(self, mode: str):
        """auto | stereo | mono"""
        if self._tb and hasattr(self._tb, "wbfm"):
            try:
                if mode == "mono":
                    self._tb.wbfm.set_audio_decim(1)  # placeholder; see flowgraph
                # Full stereo blend control is via wbfm_receive stereo_threshold
                # Expose as a future enhancement
            except Exception as e:
                logger.debug("set_stereo_mode: %s", e)

    def stop(self):
        self._running = False
        self._stop_flowgraph()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self):
        try:
            self._build_and_run()
        except ImportError as e:
            logger.error(
                "GNU Radio Python modules not found in venv. "
                "Recreate the venv with: python3 -m venv --system-site-packages .venv\n"
                "Then verify: sudo apt-get install gnuradio gr-osmosdr gr-rds\n"
                "Import error: %s",
                e,
            )
        except Exception as e:
            logger.exception("GNU Radio flowgraph error: %s", e)

    def _build_and_run(self):
        from gnuradio import gr, analog, blocks, filter as gr_filter
        import osmosdr

        tb = gr.top_block()
        self._tb = tb

        # Source
        src = osmosdr.source(f"rtl={self._device_index}")
        src.set_sample_rate(self.SAMPLE_RATE)
        src.set_center_freq(self._freq_hz)
        src.set_freq_corr(self._ppm_correction, 0)
        if self._gain == "auto":
            src.set_gain_mode(True, 0)
        else:
            src.set_gain_mode(False, 0)
            src.set_gain(float(self._gain), 0)
        tb.src = src

        # Channel filter: decimate 2MHz → DEMOD_RATE (200kHz), apply bandwidth filter.
        # wfm_rcv_pll requires complex input at exactly DEMOD_RATE.
        bw = 130_000 if self._bandwidth == "narrow" else 200_000
        chan_filter = gr_filter.freq_xlating_fir_filter_ccc(
            self.SAMPLE_RATE // self.DEMOD_RATE,   # decimation = 10
            gr_filter.firdes.low_pass(1.0, self.SAMPLE_RATE, bw / 2, 10_000),
            0,
            self.SAMPLE_RATE,
        )

        # FM stereo demodulator with PLL pilot detection (GR 3.10+).
        # Replaces the removed wbfm_receive block.
        # de-emphasis is applied internally — pass tau here, not as a separate block.
        # Input: complex at DEMOD_RATE. Output: (0) L float, (1) R float at AUDIO_RATE.
        tau = self._deemphasis_us * 1e-6
        wbfm = analog.wfm_rcv_pll(
            demod_rate=self.DEMOD_RATE,
            audio_decimation=self.AUDIO_DECIM,
            deemph_tau=tau,
        )
        tb.wbfm = wbfm

        # Float→short for PCM output
        to_short_l = blocks.float_to_short(1, 32767)
        to_short_r = blocks.float_to_short(1, 32767)

        # Interleave L+R into stereo
        interleave = blocks.interleave(gr.sizeof_short)

        # Write stereo PCM to FIFO
        sink = blocks.file_sink(gr.sizeof_short, self._fifo_path, False)
        sink.set_unbuffered(True)

        # Audio connections
        tb.connect(src, chan_filter, wbfm)
        tb.connect((wbfm, 0), to_short_l, (interleave, 0))
        tb.connect((wbfm, 1), to_short_r, (interleave, 1))
        tb.connect(interleave, sink)

        # RDS path (best-effort — skip if gr-rds not installed)
        self._connect_rds(tb, src)

        logger.info("Starting GNU Radio FM flowgraph at %.3f MHz", self._freq_hz / 1e6)
        tb.start()

        # Keep running until stop() is called
        while self._running:
            import time
            time.sleep(0.5)

        tb.stop()
        tb.wait()
        self._tb = None

    def _connect_rds(self, tb, src):
        try:
            from gnuradio import gr, analog, filter as gr_filter, digital
            import rds

            # Separate FM demod at lower rate for RDS
            rds_decim = int(self.SAMPLE_RATE / 250_000)
            rds_filter = gr_filter.freq_xlating_fir_filter_ccc(
                rds_decim,
                gr_filter.firdes.low_pass(1.0, self.SAMPLE_RATE, 100_000, 10_000),
                0,
                self.SAMPLE_RATE,
            )
            fm_demod = analog.quadrature_demod_cf(250_000 / (2 * 3.14159 * 75_000))
            rds_decoder = rds.decoder(False, False)
            rds_parser = rds.parser(False, False, 0)

            tb.connect(src, rds_filter, fm_demod, rds_decoder)
            tb.msg_connect(rds_decoder, "out", rds_parser, "in")
            tb.msg_connect(rds_parser, "out", tb.message_port_register_hier_out("rds_out"))

            # Subscribe to RDS messages
            rds_parser.set_msg_handler(
                "out",
                lambda msg: self._handle_rds_msg(msg),
            )
            logger.info("gr-rds connected")
        except Exception as e:
            logger.info("gr-rds not available, skipping RDS: %s", e)

    def _handle_rds_msg(self, msg):
        if not self._rds_callback:
            return
        try:
            import pmt
            d = pmt.to_python(msg)
            if isinstance(d, dict):
                self._rds_callback({
                    "ps": d.get("ps", ""),
                    "rt": d.get("rt", ""),
                    "pty": _rds_pty_name(d.get("pty", 0)),
                    "pi": d.get("pi", ""),
                })
        except Exception as e:
            logger.debug("RDS message parse error: %s", e)

    def _stop_flowgraph(self):
        self._running = False
        if self._tb:
            try:
                self._tb.stop()
                self._tb.wait()
            except Exception:
                pass
            self._tb = None


# RDS PTY codes (RBDS — North America)
_PTY_NAMES = {
    0: "", 1: "News", 2: "Information", 3: "Sports", 4: "Talk", 5: "Rock",
    6: "Classic Rock", 7: "Adult Hits", 8: "Soft Rock", 9: "Top 40",
    10: "Country", 11: "Oldies", 12: "Soft", 13: "Nostalgia", 14: "Jazz",
    15: "Classical", 16: "R&B", 17: "Soft R&B", 18: "Foreign Language",
    19: "Religious Music", 20: "Religious Talk", 21: "Personality",
    22: "Public", 23: "College", 29: "Weather", 30: "Emergency Test",
    31: "Emergency",
}


def _rds_pty_name(code: int) -> str:
    return _PTY_NAMES.get(code, "")
