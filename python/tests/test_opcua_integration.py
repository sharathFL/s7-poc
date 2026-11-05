"""Integration test: real asyncua driver vs an IN-PROCESS GENERIC OPC UA server.
Verifies our driver code only. This is NOT an S7-1500 emulation."""
import asyncio

import pytest
from asyncua import Server, ua
from app.config import parse_config
from app.drivers.opcua_driver import OpcUaDriver

pytestmark = pytest.mark.opcua_server
PORT = 48401


@pytest.fixture
async def server():
    s = Server()
    await s.init()
    s.set_endpoint(f"opc.tcp://127.0.0.1:{PORT}")
    idx = await s.register_namespace("urn:test")
    obj = await s.nodes.objects.add_object(idx, "PocData")
    t = await obj.add_variable(ua.NodeId("Temperature", idx), "Temperature", ua.Variant(35.5, ua.VariantType.Float))
    c = await obj.add_variable(ua.NodeId("ProductionCount", idx), "ProductionCount", ua.Variant(7, ua.VariantType.Int32))
    async with s:
        yield s, t, c


def cfg():
    return parse_config({"devices": [{"id": "d", "ip": "x", "opcua": {"endpoint": f"opc.tcp://127.0.0.1:{PORT}", "timeout_ms": 2000},
                                      "s7comm": {"enabled": False},
                                      "tags": [{"name": "Temperature", "type": "REAL", "opcua_node": "ns=2;s=Temperature"},
                                               {"name": "ProductionCount", "type": "DINT", "opcua_node": "ns=2;s=ProductionCount"}]}]}).devices[0]


async def test_read_browse_subscribe(server):
    _, temp, _ = server
    drv = OpcUaDriver(cfg())
    await drv.connect()
    try:
        rs = {r.tag: r for r in await drv.read()}
        assert rs["Temperature"].value == 35.5 and rs["Temperature"].datatype == "Float"
        assert rs["ProductionCount"].value == 7 and rs["ProductionCount"].datatype == "Int32"
        assert await drv.healthy()
        names = {n["browse_name"] for n in await drv.browse()}
        assert {"PocData", "Temperature"} <= names

        got = []
        await drv.subscribe(lambda r: got.append(r))
        await temp.write_value(ua.Variant(40.0, ua.VariantType.Float))
        for _ in range(30):
            if any(r.value == 40.0 for r in got):
                break
            await asyncio.sleep(0.1)
        assert any(r.value == 40.0 for r in got)
    finally:
        await drv.disconnect()


async def test_connect_failure_is_driver_error():
    from app.drivers.base import DriverError
    d = cfg()
    d.opcua.endpoint = "opc.tcp://127.0.0.1:1"
    with pytest.raises(DriverError):
        await OpcUaDriver(d).connect()
