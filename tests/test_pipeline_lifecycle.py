"""RadioPipeline lifecycle: device setup failures, retune threading,
encoder shutdown, and RDS decoder binding.  No hardware — rtlsdr is faked."""

import asyncio
import sys
import threading
import types

import numpy as np

import backend.sdr.pipeline as pl
from backend.metadata import MetadataState
from backend.sdr.pipeline import RadioPipeline


class _FakeStreams:
    def __init__(self):
        self.chunks = []

    async def wait_for_clients(self):
        return

    def is_active(self):
        return True

    def broadcast(self, chunk):
        self.chunks.append(chunk)


class _FakeRtlSdr:
    """Records lifecycle; optionally fails during setup."""
    instances = []
    fail_setup = 0          # number of opens whose setup raises

    def __init__(self, device_index=0):
        self.device_index = device_index
        self.closed = False
        self.freq_writes = []
        _FakeRtlSdr.instances.append(self)
        self._fail = _FakeRtlSdr.fail_setup > 0
        if self._fail:
            _FakeRtlSdr.fail_setup -= 1

    @property
    def sample_rate(self):
        return 0

    @sample_rate.setter
    def sample_rate(self, v):
        if self._fail:
            raise OSError("usb_claim_interface error -6")

    @property
    def center_freq(self):
        return self.freq_writes[-1] if self.freq_writes else 0

    @center_freq.setter
    def center_freq(self, v):
        self.freq_writes.append(v)

    gain_values = [297]
    gain = None

    def set_direct_sampling(self, v):
        pass

    def stream(self, n):
        async def gen():
            while True:
                await asyncio.sleep(0.01)
                yield np.zeros(n, np.complex64)
        return gen()

    async def stop(self):
        pass

    def close(self):
        self.closed = True


def _install_fake_rtlsdr(monkeypatch):
    _FakeRtlSdr.instances = []
    _FakeRtlSdr.fail_setup = 0
    mod = types.ModuleType("rtlsdr")
    mod.RtlSdr = _FakeRtlSdr
    monkeypatch.setitem(sys.modules, "rtlsdr", mod)


def _pipeline(cfg=None):
    meta = MetadataState({})
    meta.update_tune(98.1e6, "fm")
    return RadioPipeline(cfg or {}, meta, _FakeStreams()), meta


def test_setup_failure_releases_device_and_retries(monkeypatch):
    _install_fake_rtlsdr(monkeypatch)
    monkeypatch.setattr(pl, "_OPEN_RETRY_MIN_S", 0.01)
    _FakeRtlSdr.fail_setup = 2

    async def run():
        p, meta = _pipeline({"sdr": {"device_index": 1}})
        await p.start(98.1e6, "fm")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(_FakeRtlSdr.instances) >= 3:
                break
        await p.close()

    asyncio.run(run())
    devs = _FakeRtlSdr.instances
    assert len(devs) >= 3                       # two failures, then success
    assert devs[0].closed and devs[1].closed    # failed setups released USB
    assert all(d.device_index == 1 for d in devs)


def test_retune_defers_tuner_write_to_session_loop(monkeypatch):
    _install_fake_rtlsdr(monkeypatch)

    async def run():
        p, meta = _pipeline()
        await p.start(98.1e6, "fm")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if p._sdr is not None:
                break
        sdr = p._sdr
        writes_before = len(sdr.freq_writes)
        await p.retune(101.1e6)
        immediate = sdr.freq_writes[writes_before:]
        for _ in range(100):
            await asyncio.sleep(0.01)
            if sdr.freq_writes[-1] == 101.1e6:
                break
        await p.close()
        return immediate, sdr.freq_writes

    immediate, writes = asyncio.run(run())
    assert immediate == []            # nothing written from the handler
    assert writes[-1] == 101.1e6      # applied by the loop


def test_encoder_closed_on_dsp_thread():
    p, meta = _pipeline()
    seen = {}

    class _Enc:
        def close(self):
            seen["thread"] = threading.current_thread().name

    async def run():
        await p._close_encoder(_Enc())
        await p.close()

    asyncio.run(run())
    assert seen["thread"].startswith("sdr-dsp")


def test_rds_callbacks_carry_their_decoders_generation():
    # A decoder replaced by a retune must keep reporting its own (old)
    # generation, so MetadataState drops its late groups.
    p, meta = _pipeline()
    got = []
    p._on_rds = lambda data, gen: got.append(gen)
    old = p._new_rds()
    p._meta_gen += 1
    new = p._new_rds()
    old._cb({})
    new._cb({})
    asyncio.run(p.close())
    assert got == [p._meta_gen - 1, p._meta_gen]
