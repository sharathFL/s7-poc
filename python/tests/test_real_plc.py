"""REAL-HARDWARE tests. Skipped unless S7_REAL_PLC_IP is set. Read-only (db_read only).
    S7_REAL_PLC_IP=192.168.1.10 pytest -m plc
"""
import os

import pytest
from app.config import parse_config
from app.drivers.s7_driver import S7Driver

pytestmark = [pytest.mark.plc, pytest.mark.skipif(not os.environ.get("S7_REAL_PLC_IP"), reason="no real PLC configured")]


async def test_read_five_tags():
    ip = os.environ["S7_REAL_PLC_IP"]
    dev = parse_config({"devices": [{"id": "plc", "ip": ip,
        "s7comm": {"rack": int(os.environ.get("S7_RACK", 0)), "slot": int(os.environ.get("S7_SLOT", 1))},
        "tags": [{"name": "Temperature", "type": "REAL", "s7_address": "DB10.DBD0"},
                 {"name": "Pressure", "type": "REAL", "s7_address": "DB10.DBD4"},
                 {"name": "MotorSpeed", "type": "REAL", "s7_address": "DB10.DBD8"},
                 {"name": "MotorRunning", "type": "BOOL", "s7_address": "DB10.DBX12.0"},
                 {"name": "ProductionCount", "type": "DINT", "s7_address": "DB10.DBD14"}]}]}).devices[0]
    d = S7Driver(dev)
    await d.connect()
    try:
        rs = await d.read()
        assert len(rs) == 5 and all(r.quality.value == "GOOD" for r in rs), [r.to_dict() for r in rs]
    finally:
        await d.disconnect()
