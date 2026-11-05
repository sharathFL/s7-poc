"""Generic OPC UA test server (asyncua). NOT an S7-1500 emulation and has no S7comm endpoint.
Publishes the PoC tags under ns=2;s=<Tag> with deterministic simulated values."""
import asyncio
import logging
import sys
import time

from asyncua import Server, ua

sys.path.insert(0, "/app")
from app.drivers.mock_driver import simulated_value  # noqa: E402

TAGS = [("Temperature", "REAL"), ("Pressure", "REAL"), ("MotorSpeed", "REAL"),
        ("MotorRunning", "BOOL"), ("ProductionCount", "DINT")]


async def main(port: int = 4840):
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{port}")
    server.set_server_name("S7-PoC generic OPC UA test server")
    idx = await server.register_namespace("urn:s7poc:sim")   # -> ns=2
    obj = await server.nodes.objects.add_object(idx, "PocData")
    nodes = {}
    for name, t in TAGS:
        init = simulated_value(name, t, time.time())
        vt = {"REAL": ua.VariantType.Float, "BOOL": ua.VariantType.Boolean, "DINT": ua.VariantType.Int32}[t]
        n = await obj.add_variable(ua.NodeId(name, idx), name, ua.Variant(init, vt))
        nodes[name] = (n, vt, t)
    async with server:
        while True:
            now = time.time()
            for name, (n, vt, t) in nodes.items():
                await n.write_value(ua.Variant(simulated_value(name, t, now), vt))
            await asyncio.sleep(0.5)

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
