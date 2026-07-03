"""
AAC-LC streaming via PyAV + per-client asyncio queues.

AacEncoder wraps PyAV to produce ADTS-framed AAC bytes from float32 PCM.
StreamingManager distributes chunks to all connected HTTP clients.

ADTS (Audio Data Transport Stream) is a self-framing raw AAC container —
each frame has a 7-byte sync header so browsers can start decoding at any
point in the stream, matching the behaviour of Icecast / SHOUTcast MP3.
"""

import asyncio
import io
import logging
from typing import Optional

import av
import numpy as np

logger = logging.getLogger(__name__)

AUDIO_RATE      = 48_000
BITRATE_STEREO  = 128_000  # 128 kbps AAC-LC; preserves 0-15 kHz FM bandwidth through encoder
BITRATE_MONO    = 48_000   # 48 kbps mono for AM / scanner


# ---------------------------------------------------------------------------
# Write-only buffer that PyAV muxes into; we drain it after each encode call.
# ---------------------------------------------------------------------------

class _DrainBuffer(io.RawIOBase):
    def __init__(self):
        super().__init__()
        self._chunks: list[bytes] = []

    def write(self, data) -> int:           # type: ignore[override]
        if data:
            self._chunks.append(bytes(data))
        return len(data) if data else 0

    def drain(self) -> bytes:
        if not self._chunks:
            return b""
        result = b"".join(self._chunks)
        self._chunks.clear()
        return result

    def readable(self)  -> bool: return False
    def writable(self)  -> bool: return True
    def seekable(self)  -> bool: return False


# ---------------------------------------------------------------------------
# AAC encoder
# ---------------------------------------------------------------------------

class AacEncoder:
    """
    Encodes float32 PCM to AAC-LC in ADTS container.

    Thread-safe for encode() — called from a ThreadPoolExecutor.
    All internal state is owned by the calling thread.
    """

    def __init__(self, stereo: bool = True):
        self._stereo   = stereo
        self._channels = 2 if stereo else 1
        self._bitrate  = BITRATE_STEREO if stereo else BITRATE_MONO
        self._layout   = "stereo" if stereo else "mono"
        self._pts      = 0

        self._buf       = _DrainBuffer()
        self._container = av.open(self._buf, format="adts", mode="w")
        # Pass layout to add_stream — PyAV 12+ made .channels read-only;
        # channel count is derived from the layout, not set independently.
        self._stream    = self._container.add_stream(
            "aac", rate=AUDIO_RATE, layout=self._layout
        )
        self._stream.bit_rate = self._bitrate

    def encode(self, *arrays: np.ndarray) -> bytes:
        """
        Encode one PCM block.  Arrays are float32 in [-1.0, 1.0].
        Stereo: encode(left, right)   Mono: encode(mono)
        Returns ADTS-framed AAC bytes (may be empty if encoder is buffering).
        """
        n = len(arrays[0])
        frame = av.AudioFrame(format="fltp", layout=self._layout, samples=n)
        frame.sample_rate = AUDIO_RATE
        frame.pts         = self._pts
        self._pts        += n

        for i, arr in enumerate(arrays):
            f32 = np.clip(arr, -1.0, 1.0).astype(np.float32)
            frame.planes[i].update(f32.tobytes())

        for packet in self._stream.encode(frame):
            self._container.mux(packet)

        return self._buf.drain()

    def flush(self) -> bytes:
        for packet in self._stream.encode(None):
            self._container.mux(packet)
        return self._buf.drain()

    def close(self):
        try:
            self.flush()
            self._container.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Multi-client streaming manager
# ---------------------------------------------------------------------------

class StreamingManager:
    """
    Each connected browser gets its own asyncio.Queue of AAC chunks.
    Chunks are broadcast to all queues; full queues are silently dropped
    (live radio — no seeking, slow clients just skip a frame).
    """

    MAX_QUEUE = 8    # ~8 blocks ≈ 1.75 s of audio buffer per client (blocks are 218 ms each)

    # Seconds with zero clients before DSP is suspended.  Short enough that a
    # page refresh doesn't cause a brief silence gap on reconnect; long enough
    # that an active recording keeps DSP running even if the browser tab closes.
    IDLE_GRACE_SECS = 30

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()
        # Passive subscribers (e.g. the Icecast pusher in on-demand mode)
        # receive broadcast chunks but do not count as listeners: they never
        # set the active event or block the idle countdown, so they cannot
        # keep the DSP running on their own.
        self._passive: set[asyncio.Queue] = set()
        # Set when at least one client is connected; cleared after IDLE_GRACE_SECS
        # with no clients.  pipeline.py awaits this before dispatching to the DSP
        # executor so all demodulation / encoding is suspended while idle.
        self._active_event: asyncio.Event = asyncio.Event()
        self._idle_task: Optional[asyncio.Task] = None

    def new_client(self, passive: bool = False) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE)
        if passive:
            self._passive.add(q)
            return q
        self._clients.add(q)
        # Cancel any pending idle countdown and immediately mark DSP as needed.
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None
        self._active_event.set()
        logger.debug("Audio client connected (%d total)", len(self._clients))
        return q

    def remove_client(self, q: asyncio.Queue):
        if q in self._passive:
            self._passive.discard(q)
            return
        self._clients.discard(q)
        logger.debug("Audio client disconnected (%d total)", len(self._clients))
        if not self._clients and not (self._idle_task and not self._idle_task.done()):
            self._idle_task = asyncio.ensure_future(self._idle_after_grace())

    async def _idle_after_grace(self):
        """Clear the active event after a grace period, suspending DSP."""
        await asyncio.sleep(self.IDLE_GRACE_SECS)
        if not self._clients:
            self._active_event.clear()
            logger.info("No audio clients for %ds — DSP suspended", self.IDLE_GRACE_SECS)

    async def wait_for_clients(self):
        """Await until at least one client is connected (or reconnects)."""
        await self._active_event.wait()

    def is_active(self) -> bool:
        """Non-blocking check: True if DSP should run (event is set)."""
        return self._active_event.is_set()

    def broadcast(self, chunk: bytes):
        if not chunk or not (self._clients or self._passive):
            return
        dead: set = set()
        for q in self._clients | self._passive:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                pass          # drop for slow clients
            except Exception:
                dead.add(q)
        self._clients -= dead
        self._passive -= dead

    def drain_all(self):
        """Flush all client queues on retune to prevent stale audio."""
        for q in self._clients | self._passive:
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    @property
    def client_count(self) -> int:
        return len(self._clients)
