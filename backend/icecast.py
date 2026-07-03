"""
Icecast2 source client — pushes the AAC stream to an Icecast mount.

Speaks the legacy SOURCE protocol (HTTP/1.0), which every Icecast version
and most Icecast-compatible servers accept:

    SOURCE /mount HTTP/1.0
    Authorization: Basic base64(source:password)
    Content-Type: audio/aac
    ...
    <raw ADTS AAC bytes, forever>

Now-playing metadata is pushed out-of-band via the standard
/admin/metadata?mode=updinfo endpoint (source credentials are authorized
for their own mount), so Icecast-side listeners (VLC, foobar2000, Sonos…)
see artist/title from RDS or HD Radio.

Two modes, controlled by `icecast.keep_alive` in settings.yaml:

  keep_alive: false (default) — on-demand.  The pusher subscribes to the
    StreamingManager as a *passive* client and only connects to Icecast
    while a real listener or recording keeps the DSP running.  The mount
    disappears while Squelch is idle; DSP idle-suspend keeps working.

  keep_alive: true — always-on.  The pusher counts as a real client, so
    the DSP runs (and the mount stays live) whenever a station is tuned,
    even with no browser listeners.  Costs continuous CPU on the Pi.
"""

import asyncio
import base64
import logging
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)


class IcecastPusher:
    RECONNECT_MIN_SECS = 2.0
    RECONNECT_MAX_SECS = 60.0
    CHUNK_TIMEOUT_SECS = 10.0    # poll interval while the queue is empty
    METADATA_POLL_SECS = 3.0

    def __init__(self, cfg: dict, metadata, streams):
        self._host       = cfg.get("host", "localhost")
        self._port       = int(cfg.get("port", 8001))
        self._mount      = cfg.get("mount", "/radio")
        self._password   = str(cfg.get("source_password", ""))
        self._keep_alive = bool(cfg.get("keep_alive", False))
        self._meta       = metadata
        self._streams    = streams
        self._task: Optional[asyncio.Task] = None
        if not self._mount.startswith("/"):
            self._mount = "/" + self._mount

    def start(self):
        self._task = asyncio.create_task(self._run(), name="icecast-pusher")
        logger.info("Icecast output enabled: %s:%d%s (%s)",
                    self._host, self._port, self._mount,
                    "always-on" if self._keep_alive else "on-demand")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------

    async def _run(self):
        backoff = self.RECONNECT_MIN_SECS
        while True:
            if not self._keep_alive:
                # On-demand: don't touch Icecast until a real listener or
                # recording has the DSP running.
                await self._streams.wait_for_clients()
            try:
                await self._stream_once()
                backoff = self.RECONNECT_MIN_SECS   # clean disconnect (went idle)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Icecast connection failed: %s — retrying in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.RECONNECT_MAX_SECS)

    async def _stream_once(self):
        """One connection lifetime: handshake, then forward chunks until
        cancelled, the socket dies, or (on-demand mode) the DSP goes idle."""
        reader, writer = await asyncio.open_connection(self._host, self._port)
        q = self._streams.new_client(passive=not self._keep_alive)
        meta_task: Optional[asyncio.Task] = None
        try:
            await self._handshake(reader, writer)
            logger.info("Icecast source connected: %s:%d%s", self._host, self._port, self._mount)
            meta_task = asyncio.create_task(self._metadata_loop())

            while True:
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=self.CHUNK_TIMEOUT_SECS)
                except asyncio.TimeoutError:
                    if not self._keep_alive and not self._streams.is_active():
                        logger.info("Icecast source disconnecting — no listeners")
                        return
                    continue   # tuned but silent (or idle in keep-alive mode)
                writer.write(chunk)
                await writer.drain()
        finally:
            self._streams.remove_client(q)
            if meta_task:
                meta_task.cancel()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handshake(self, reader, writer):
        creds = base64.b64encode(f"source:{self._password}".encode()).decode()
        request = (
            f"SOURCE {self._mount} HTTP/1.0\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            f"Authorization: Basic {creds}\r\n"
            "User-Agent: squelch\r\n"
            "Content-Type: audio/aac\r\n"
            "Ice-Name: Squelch\r\n"
            "Ice-Description: SDR Radio Stream\r\n"
            "Ice-Public: 0\r\n"
            "\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if b" 200 " not in status and not status.rstrip().endswith(b" 200"):
            raise ConnectionError(f"Icecast rejected source: {status.decode(errors='replace').strip()!r}")
        # Drain remaining response headers
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if line in (b"\r\n", b"\n", b""):
                break

    # ------------------------------------------------------------------
    # Now-playing metadata
    # ------------------------------------------------------------------

    async def _metadata_loop(self):
        """Push artist/title to Icecast whenever it changes (poll-based so we
        don't need hooks inside MetadataState)."""
        last_song = None
        try:
            while True:
                song = self._current_song()
                if song and song != last_song:
                    if await self._push_metadata(song):
                        last_song = song
                await asyncio.sleep(self.METADATA_POLL_SECS)
        except asyncio.CancelledError:
            pass

    def _current_song(self) -> Optional[str]:
        if self._meta.artist and self._meta.title:
            return f"{self._meta.artist} - {self._meta.title}"
        if self._meta.title:
            return self._meta.title
        return self._meta.station_name

    async def _push_metadata(self, song: str) -> bool:
        params = urllib.parse.urlencode(
            {"mode": "updinfo", "mount": self._mount, "song": song}
        )
        creds = base64.b64encode(f"source:{self._password}".encode()).decode()
        request = (
            f"GET /admin/metadata?{params} HTTP/1.0\r\n"
            f"Host: {self._host}:{self._port}\r\n"
            f"Authorization: Basic {creds}\r\n"
            "User-Agent: squelch\r\n"
            "\r\n"
        )
        try:
            reader, writer = await asyncio.open_connection(self._host, self._port)
            try:
                writer.write(request.encode())
                await writer.drain()
                status = await asyncio.wait_for(reader.readline(), timeout=5.0)
                ok = b" 200 " in status or status.rstrip().endswith(b" 200")
                if not ok:
                    logger.debug("Icecast metadata update rejected: %s", status)
                return ok
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Icecast metadata update failed: %s", e)
            return False
