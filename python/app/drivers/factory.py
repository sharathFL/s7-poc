from __future__ import annotations

from ..config import DeviceConfig
from .base import Driver
from .mock_driver import MockDriver


def build_drivers(dev: DeviceConfig) -> list[Driver]:
    """One driver instance per enabled protocol. Register new protocols (e.g. Modbus TCP) here."""
    out: list[Driver] = []
    if dev.mock:
        if dev.s7.enabled:
            out.append(MockDriver(dev, "s7comm"))
        if dev.opcua.enabled:
            out.append(MockDriver(dev, "opcua"))
        return out
    if dev.s7.enabled:
        from .s7_driver import S7Driver  # lazy: mock mode must not need snap7
        out.append(S7Driver(dev))
    if dev.opcua.enabled:
        from .opcua_driver import OpcUaDriver
        out.append(OpcUaDriver(dev))
    return out
