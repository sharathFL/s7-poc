"""Unified, protocol-independent data model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Quality(str, Enum):
    GOOD = "GOOD"
    STALE = "STALE"                  # value is old (no recent successful read)
    INVALID = "INVALID"              # read ok but item bad (bad status code, decode error)
    COMM_FAILURE = "COMM_FAILURE"    # connection / transport failure


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if ts else None


@dataclass
class Reading:
    device_id: str
    protocol: str            # s7comm | opcua | mock-s7comm | mock-opcua | (future) modbus
    tag: str
    datatype: str
    value: Any = None
    timestamp: datetime = field(default_factory=utcnow)   # when WE sampled
    quality: Quality = Quality.GOOD
    error: str | None = None
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # protocol specific (address, node id, source ts...)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "protocol": self.protocol,
            "tag": self.tag,
            "value": self.value,
            "datatype": self.datatype,
            "timestamp": iso(self.timestamp),
            "quality": self.quality.value,
            "error": self.error,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 2),
            "metadata": self.metadata,
        }


@dataclass
class TagConfig:
    name: str
    type: str
    s7_address: str | None = None
    opcua_node: str | None = None
