"""IcecastPusher tests against a fake in-process Icecast server."""

import asyncio
import base64

import pytest

from backend.icecast import IcecastPusher
from backend.metadata import MetadataState
from backend.streaming import StreamingManager

PASSWORD = "hackme"


class FakeIcecast:
    """Accepts SOURCE and /admin/metadata requests like Icecast 2.x."""

    def __init__(self):
        self.server = None
        self.port = None
        self.source_headers: list[dict] = []
        self.body = bytearray()
        self.metadata_requests: list[str] = []
        self.source_connections = 0
        self.source_disconnects = 0

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        request_line = (await reader.readline()).decode()
        headers = {}
        while True:
            line = (await reader.readline()).decode()
            if line in ("\r\n", "\n", ""):
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

        expected = base64.b64encode(f"source:{PASSWORD}".encode()).decode()
        authorized = headers.get("authorization") == f"Basic {expected}"

        if request_line.startswith("SOURCE "):
            if not authorized:
                writer.write(b"HTTP/1.0 401 Unauthorized\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            self.source_connections += 1
            self.source_headers.append(headers)
            writer.write(b"HTTP/1.0 200 OK\r\n\r\n")
            await writer.drain()
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    self.body.extend(data)
            finally:
                self.source_disconnects += 1
        elif request_line.startswith("GET /admin/metadata"):
            if authorized:
                self.metadata_requests.append(request_line.split(" ")[1])
                writer.write(b"HTTP/1.0 200 OK\r\n\r\nUpdated\r\n")
            else:
                writer.write(b"HTTP/1.0 401 Unauthorized\r\n\r\n")
            await writer.drain()

        writer.close()


@pytest.fixture
async def fake_ice():
    server = FakeIcecast()
    await server.start()
    yield server
    await server.stop()


def make_pusher(fake_ice, streams, meta=None, keep_alive=True, password=PASSWORD):
    pusher = IcecastPusher(
        {"host": "127.0.0.1", "port": fake_ice.port, "mount": "radio",
         "source_password": password, "keep_alive": keep_alive},
        meta or MetadataState(),
        streams,
    )
    pusher.RECONNECT_MIN_SECS = 0.05   # fast retries in tests
    pusher.CHUNK_TIMEOUT_SECS = 0.05
    pusher.METADATA_POLL_SECS = 0.05
    return pusher


async def wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        assert asyncio.get_event_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------

async def test_handshake_and_stream_forwarding(fake_ice):
    streams = StreamingManager()
    pusher = make_pusher(fake_ice, streams)
    pusher.start()
    try:
        await wait_for(lambda: fake_ice.source_connections == 1)
        assert fake_ice.source_headers[0]["content-type"] == "audio/aac"

        streams.broadcast(b"\xff\xf1AAC-CHUNK-1")
        streams.broadcast(b"\xff\xf1AAC-CHUNK-2")
        await wait_for(lambda: b"AAC-CHUNK-2" in fake_ice.body)
        assert b"AAC-CHUNK-1" in fake_ice.body
    finally:
        await pusher.stop()


async def test_bad_password_retries_without_crashing(fake_ice):
    streams = StreamingManager()
    pusher = make_pusher(fake_ice, streams, password="wrong")
    pusher.start()
    try:
        # Rejected handshakes must not increment source_connections,
        # and the pusher keeps retrying with backoff instead of dying.
        await asyncio.sleep(0.3)
        assert fake_ice.source_connections == 0
        assert not pusher._task.done()
    finally:
        await pusher.stop()


async def test_on_demand_mode_follows_listeners(fake_ice, monkeypatch):
    monkeypatch.setattr(StreamingManager, "IDLE_GRACE_SECS", 0.05)
    streams = StreamingManager()
    pusher = make_pusher(fake_ice, streams, keep_alive=False)
    pusher.start()
    try:
        # No listeners yet: the pusher must not connect
        await asyncio.sleep(0.2)
        assert fake_ice.source_connections == 0

        # First real listener arrives → pusher connects
        q = streams.new_client()
        await wait_for(lambda: fake_ice.source_connections == 1)

        # Passive subscription must not keep the DSP active by itself:
        # when the listener leaves, the pusher disconnects after the grace.
        streams.remove_client(q)
        await wait_for(lambda: fake_ice.source_disconnects == 1)

        # And a returning listener brings the mount back
        streams.new_client()
        await wait_for(lambda: fake_ice.source_connections == 2)
    finally:
        await pusher.stop()


async def test_metadata_pushed_on_change(fake_ice):
    streams = StreamingManager()
    meta = MetadataState()
    pusher = make_pusher(fake_ice, streams, meta=meta)
    pusher.start()
    try:
        await wait_for(lambda: fake_ice.source_connections == 1)

        meta.artist, meta.title = "Boards of Canada", "Roygbiv"
        await wait_for(lambda: len(fake_ice.metadata_requests) >= 1)
        assert "Boards+of+Canada+-+Roygbiv" in fake_ice.metadata_requests[0]
        assert "mode=updinfo" in fake_ice.metadata_requests[0]

        # Unchanged metadata is not re-sent
        await asyncio.sleep(0.2)
        n = len(fake_ice.metadata_requests)
        await asyncio.sleep(0.2)
        assert len(fake_ice.metadata_requests) == n
    finally:
        await pusher.stop()


async def test_passive_clients_excluded_from_activity():
    streams = StreamingManager()
    pq = streams.new_client(passive=True)
    assert not streams.is_active()          # passive alone doesn't wake DSP

    streams.broadcast(b"x")
    assert pq.get_nowait() == b"x"          # …but still receives chunks

    streams.remove_client(pq)
    assert pq not in streams._passive
