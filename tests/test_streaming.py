import asyncio

import numpy as np
import pytest

from backend.streaming import AacEncoder, StreamingManager, AUDIO_RATE


def _tone(n=4096, freq=1000.0):
    t = np.arange(n) / AUDIO_RATE
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# AAC encoder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stereo", [True, False])
def test_encoder_produces_adts_frames(stereo):
    enc = AacEncoder(stereo=stereo)
    out = b""
    for _ in range(10):
        args = (_tone(), _tone(freq=2000)) if stereo else (_tone(),)
        out += enc.encode(*args)
    out += enc.flush()
    enc.close()

    assert len(out) > 0
    # ADTS syncword: 12 set bits at every frame start
    assert out[0] == 0xFF and (out[1] & 0xF0) == 0xF0


def test_encoder_handles_out_of_range_pcm():
    enc = AacEncoder(stereo=False)
    loud = (10.0 * _tone()).astype(np.float32)  # must be clipped, not crash
    for _ in range(5):
        enc.encode(loud)
    enc.close()


# ---------------------------------------------------------------------------
# StreamingManager
# ---------------------------------------------------------------------------

async def test_broadcast_reaches_all_clients():
    sm = StreamingManager()
    q1, q2 = sm.new_client(), sm.new_client()
    sm.broadcast(b"chunk")
    assert q1.get_nowait() == b"chunk"
    assert q2.get_nowait() == b"chunk"


async def test_full_queue_drops_chunk_without_error():
    sm = StreamingManager()
    q = sm.new_client()
    for i in range(sm.MAX_QUEUE + 5):
        sm.broadcast(b"%d" % i)
    assert q.qsize() == sm.MAX_QUEUE  # extras silently dropped


async def test_drain_all_empties_queues():
    sm = StreamingManager()
    q = sm.new_client()
    sm.broadcast(b"stale")
    sm.drain_all()
    assert q.empty()


async def test_dsp_idle_suspend_and_resume(monkeypatch):
    monkeypatch.setattr(StreamingManager, "IDLE_GRACE_SECS", 0.01)
    sm = StreamingManager()

    q = sm.new_client()
    assert sm.is_active()

    sm.remove_client(q)
    await asyncio.sleep(0.05)
    assert not sm.is_active()  # DSP suspended after grace period

    sm.new_client()
    assert sm.is_active()  # reconnect resumes immediately
