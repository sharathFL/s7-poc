"""MOCK-DRIVER tests: exercise acquisition + API with simulated data. They prove the software
pipeline only; they say nothing about behaviour of a real S7-1500."""
import asyncio

import httpx
import pytest
from app.drivers.mock_driver import MockDriver
from app.main import create_app
from app.models import Quality
from app.services.acquisition import Acquisition


@pytest.fixture
async def client(cfg):
    acq = Acquisition(cfg)
    app = create_app(acq)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.4)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def test_health_devices(client):
    assert (await client.get("/health")).json()["status"] == "ok"
    d = (await client.get("/devices")).json()
    assert d[0]["id"] == "dev1" and d[0]["state"] == "ONLINE"


async def test_unified_structure(client):
    rs = (await client.get("/devices/dev1/readings")).json()
    assert len(rs) == 6  # 3 tags x 2 protocols
    keys = {"device_id", "protocol", "tag", "value", "datatype", "timestamp", "quality", "metadata"}
    for r in rs:
        assert keys <= r.keys() and r["quality"] == "GOOD"
    assert {r["protocol"] for r in rs} == {"mock-s7comm", "mock-opcua"}


async def test_latest_and_404(client):
    assert (await client.get("/devices/dev1/tags/Temperature/latest")).status_code == 200
    assert (await client.get("/devices/dev1/tags/Nope/latest")).status_code == 404
    assert (await client.get("/devices/zzz/status")).status_code == 404
    assert (await client.get("/devices/dev1/readings?protocol=modbus")).status_code == 422


async def test_comparison(client):
    c = (await client.get("/comparison?device_id=dev1")).json()
    # mock protocols sample 150 ms apart, so a counter/bool may legitimately tick between reads
    assert c["summary"]["compared"] == 3 and c["summary"]["matching"] >= 2
    assert c["summary"]["max_skew_ms"] is not None and c["summary"]["type_mismatches"] == 0


async def test_no_write_routes(client):
    from app.main import create_app as _c
    for path in ("/devices", "/devices/dev1/tags", "/comparison"):
        for m in ("post", "put", "delete", "patch"):
            assert (await getattr(client, m)(path)).status_code == 405


async def test_outage_and_recovery(cfg):
    def factory(dev):
        d = MockDriver(dev, "s7comm", outage=(0.3, 1.0))
        return [d]
    acq = Acquisition(cfg, factory)
    await acq.start()
    try:
        await asyncio.sleep(0.15)
        assert acq.latest_for("dev1", "Temperature")[0].quality is Quality.GOOD
        await asyncio.sleep(0.6)  # inside outage
        r = acq.latest_for("dev1", "Temperature")[0]
        assert r.quality is Quality.COMM_FAILURE and "outage" in r.error
        assert acq.device_status("dev1")["state"] == "OFFLINE"
        await asyncio.sleep(2.8)  # outage over + backoff
        assert acq.latest_for("dev1", "Temperature")[0].quality is Quality.GOOD
        ch = acq.channels[("dev1", "s7comm")]
        assert ch.reconnects >= 1 and ch.reads_fail >= 1
    finally:
        await acq.stop()


async def test_stale(cfg):
    acq = Acquisition(cfg)
    await acq.start()
    await asyncio.sleep(0.3)
    for t in acq._tasks:
        t.cancel()
    await asyncio.sleep(1.3)  # > max(3*0.1, 1.0) s with nothing refreshing
    assert acq.latest_for("dev1", "Temperature")[0].quality is Quality.STALE
    await acq.stop()
