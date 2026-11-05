"""Deterministic simulated driver. NOT an S7-1500 emulation: it only produces plausible values
so the pipeline, API and dashboard can be exercised without hardware."""
from __future__ import annotations

import math
import time

from ..config import DeviceConfig
from ..models import Quality, Reading, utcnow
from .base import Driver, DriverError


def simulated_value(tag: str, dtype: str, t: float):
    """Pure function of (tag, time): deterministic and identical for both mock protocols."""
    h = sum(ord(c) for c in tag)
    if dtype.upper() == "BOOL":
        return int(t // 5 + h) % 2 == 0
    if dtype.upper() in ("DINT", "INT", "UINT", "UDINT", "WORD", "DWORD"):
        return int(t) + h * 10
    base = {"Temperature": 35.0, "Pressure": 4.2, "MotorSpeed": 1450.0}.get(tag, 10.0 + h % 50)
    return round(base + math.sin(t / 10 + h) * base * 0.05, 3)


OPCUA_NAMES = {"REAL": "Float", "LREAL": "Double", "BOOL": "Boolean", "INT": "Int16", "UINT": "UInt16",
               "DINT": "Int32", "UDINT": "UInt32", "WORD": "UInt16", "DWORD": "UInt32"}


class MockDriver(Driver):
    """protocol is 'mock-s7comm' or 'mock-opcua'. `fail_after`/`recover_after` simulate outages."""

    def __init__(self, device: DeviceConfig, kind: str, outage: tuple[float, float] | None = None):
        super().__init__(device)
        self.kind = kind
        self.protocol = f"mock-{kind}"
        self.polling_ms = (device.s7 if kind == "s7comm" else device.opcua).polling_ms
        self._outage = outage  # (start_after_s, duration_s) relative to driver creation
        self._t0 = time.monotonic()
        # opcua reports a different, slightly slower latency profile
        self._lat = 4.0 if kind == "s7comm" else 9.0

    def _down(self) -> bool:
        if not self._outage:
            return False
        e = time.monotonic() - self._t0
        return self._outage[0] <= e < self._outage[0] + self._outage[1]

    async def connect(self) -> None:
        if self._down():
            raise DriverError("mock: simulated network outage")
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def read(self) -> list[Reading]:
        if self._down():
            self.connected = False
            raise DriverError("mock: simulated network outage")
        now = time.time()
        # the two protocols sample at slightly different instants (like reality)
        t = now + (0.0 if self.kind == "s7comm" else 0.15)
        out = []
        for tag in self.device.tags:
            meta = {"address": tag.s7_address} if self.kind == "s7comm" else {"node_id": tag.opcua_node}
            meta["simulated"] = True
            dt = tag.type.upper() if self.kind == "s7comm" else OPCUA_NAMES.get(tag.type.upper(), tag.type)
            out.append(Reading(self.device.id, self.protocol, tag.name, dt,
                               simulated_value(tag.name, tag.type, t), utcnow(), Quality.GOOD, None,
                               self._lat, meta))
        return out

    async def browse(self, start=None, depth=2):
        if self.kind != "opcua":
            raise NotImplementedError("mock s7comm cannot browse")
        return [{"node_id": t.opcua_node, "browse_name": t.name, "node_class": "Variable",
                 "path": f"/Simulated/{t.name}", "datatype": t.type} for t in self.device.tags]
