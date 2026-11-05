import pytest
from app.config import parse_config

RAW = {"devices": [{"id": "dev1", "ip": "127.0.0.1", "mock": True,
                    "s7comm": {"polling_ms": 100}, "opcua": {"polling_ms": 100},
                    "tags": [{"name": "Temperature", "type": "REAL", "s7_address": "DB10.DBD0", "opcua_node": "ns=2;s=Temperature"},
                             {"name": "MotorRunning", "type": "BOOL", "s7_address": "DB10.DBX12.0", "opcua_node": "ns=2;s=MotorRunning"},
                             {"name": "ProductionCount", "type": "DINT", "s7_address": "DB10.DBD14", "opcua_node": "ns=2;s=ProductionCount"}]}]}


@pytest.fixture
def cfg():
    return parse_config(RAW)
