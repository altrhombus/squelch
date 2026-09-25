"""MetadataState WebSocket fan-out: stalled clients, coalescing."""

import asyncio

import backend.metadata as md
from backend.metadata import MetadataState


class _FakeWs:
    def __init__(self, stall=False):
        self.stall = stall
        self.sent = []
        self.closed = False

    async def send_text(self, msg):
        if self.stall:
            await asyncio.sleep(3600)
        self.sent.append(msg)

    async def close(self, code=1000):
        self.closed = True


def test_stalled_client_does_not_block_others(monkeypatch):
    monkeypatch.setattr(md, "_WS_SEND_TIMEOUT_S", 0.05)

    async def run():
        meta = MetadataState({})
        good, stuck = _FakeWs(), _FakeWs(stall=True)
        meta.register_ws(good)
        meta.register_ws(stuck)
        await asyncio.wait_for(meta.broadcast(), 1.0)
        await asyncio.sleep(0)   # let the background close run
        return meta, good, stuck

    meta, good, stuck = asyncio.run(run())
    assert len(good.sent) == 1
    assert stuck not in meta._websockets and good in meta._websockets
    assert stuck.closed


def test_schedule_broadcast_coalesces_bursts():
    async def run():
        meta = MetadataState({})
        ws = _FakeWs()
        meta.register_ws(ws)
        for _ in range(50):
            meta.schedule_broadcast()
        await meta._bcast_task
        return ws

    ws = asyncio.run(run())
    # One in flight plus at most one trailing re-send — not 50
    assert 1 <= len(ws.sent) <= 2
