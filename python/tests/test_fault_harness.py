"""Fault proxy + scenario scoring tests. Use a local echo server, no PLC."""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from fault_proxy import FaultProxy, Listener  # noqa: E402
from fault_scenarios import Sample, compute_metrics, verdict  # noqa: E402


async def _echo(r, w):
    try:
        while data := await r.read(4096):
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        w.close()


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
async def rig():
    echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
    eport = echo.sockets[0].getsockname()[1]
    pport = _free_port()
    proxy = FaultProxy([Listener("t", "127.0.0.1", pport, "127.0.0.1", eport)])
    await proxy.start()
    yield proxy, pport
    await proxy.stop()
    echo.close()


async def _roundtrip(port: int, payload=b"ping", timeout=1.0) -> bytes:
    r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
    try:
        w.write(payload)
        await w.drain()
        return await asyncio.wait_for(r.read(100), timeout)
    finally:
        w.close()


async def test_normal_passes_through(rig):
    proxy, port = rig
    assert await _roundtrip(port) == b"ping"
    assert proxy.status()["listeners"][0]["up_bytes"] == 4


async def test_blacksink_keeps_connection_open_but_silent(rig):
    proxy, port = rig
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"a")
    await w.drain()
    assert await r.read(10) == b"a"
    await proxy.set_mode("blacksink")
    w.write(b"b")
    await w.drain()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(r.read(10), 0.5)  # no data, and NO close either: half-open
    assert not r.at_eof()
    assert proxy.status()["listeners"][0]["dropped_bytes"] >= 1
    # a NEW connection during blacksink also gets nothing
    with pytest.raises(asyncio.TimeoutError):
        await _roundtrip(port, timeout=0.5)
    w.close()
    await proxy.set_mode("normal")
    assert await _roundtrip(port) == b"ping"


async def test_reset_kills_live_and_new_connections(rig):
    proxy, port = rig
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"a")
    await w.drain()
    await r.read(10)
    await proxy.set_mode("reset")
    with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
        data = await asyncio.wait_for(r.read(10), 1)
        if data == b"":
            raise ConnectionResetError("closed")
    with pytest.raises((ConnectionError, asyncio.TimeoutError, OSError)):
        res = await _roundtrip(port)
        if res == b"":
            raise ConnectionResetError("closed")
    await proxy.set_mode("normal")
    assert await _roundtrip(port) == b"ping"


async def test_refuse_then_recover(rig):
    proxy, port = rig
    await proxy.set_mode("refuse")
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    assert proxy.status()["listeners"][0]["listening"] is False
    await proxy.set_mode("normal")
    assert await _roundtrip(port) == b"ping"


async def test_delay_adds_latency(rig):
    proxy, port = rig
    await proxy.set_mode("delay", 300)
    t0 = time.perf_counter()
    assert await _roundtrip(port, timeout=3) == b"ping"
    assert time.perf_counter() - t0 >= 0.55  # 300 ms each way


async def test_bad_mode_rejected(rig):
    proxy, _ = rig
    with pytest.raises(ValueError):
        await proxy.set_mode("explode")


# ---- scoring ---------------------------------------------------------------------------------
def _series(bad_from, bad_to, end=40.0, step=0.5, old=None):
    out, t = [], 0.0
    while t < end:
        bad = bad_from <= t < bad_to
        out.append(Sample(t, not bad, "COMM_FAILURE" if bad else "GOOD", old if (old and not bad and t >= 10) else 0.5))
        t += step
    return out


SC = {"detect_within_s": 5, "recover_within_s": 10, "expect_detect": True}


def test_metrics_pass():
    m = compute_metrics(_series(12, 22), 10, 20)
    assert m["baseline_ok"] and m["detect_s"] == 2.0 and m["flaps"] == 1
    assert m["recover_s"] == 2.0  # still bad until t=22 -> good at 22 (stable_n=3)
    ok, why = verdict(m, SC)
    assert ok, why


def test_metrics_detect_too_slow_and_never_recover():
    m = compute_metrics(_series(18, 99), 10, 20)
    ok, why = verdict(m, SC)
    assert not ok and any("detected after" in w for w in why) and any("never recovered" in w for w in why)


def test_stale_good_is_flagged():
    m = compute_metrics(_series(99, 99, old=30.0), 10, 20)  # never bad, but readings 30 s old
    assert m["good_but_old"] > 0
    ok, why = verdict(m, {**SC, "expect_detect": False})
    assert not ok and any("older than" in w for w in why)


def test_tolerated_fault_must_not_flag():
    m = compute_metrics(_series(99, 99), 10, 20)
    assert verdict(m, {**SC, "expect_detect": False})[0]
    m = compute_metrics(_series(12, 14), 10, 20)
    assert not verdict(m, {**SC, "expect_detect": False})[0]


def test_no_baseline_fails():
    s = _series(0, 8)
    assert not verdict(compute_metrics(s, 10, 20), SC)[0]
