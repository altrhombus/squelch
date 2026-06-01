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
BITRATE_STEREO  = 96_000   # 96 kbps AAC-LC ≈ quality of 128 kbps MP3
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

    MAX_QUEUE = 12   # ~12 blocks ≈ 660 ms of audio buffer per client

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()

    def new_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE)
        self._clients.add(q)
        logger.debug("Audio client connected (%d total)", len(self._clients))
        return q

    def remove_client(self, q: asyncio.Queue):
        self._clients.discard(q)
        logger.debug("Audio client disconnected (%d total)", len(self._clients))

    def broadcast(self, chunk: bytes):
        if not chunk or not self._clients:
            return
        dead: set = set()
        for q in self._clients:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                pass          # drop for slow clients
            except Exception:
                dead.add(q)
        self._clients -= dead

    def drain_all(self):
        """Flush all client queues on retune to prevent stale audio."""
        for q in self._clients:
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    @property
    def client_count(self) -> int:
        return len(self._clients)
