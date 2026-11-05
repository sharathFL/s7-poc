"""S7comm vs OPC UA comparison. Reads are independent samples, so values are compared with a
tolerance and the sampling-time skew is reported alongside, never assumed to be zero."""
from __future__ import annotations

import math
from typing import Any

from ..models import Reading, Quality, utcnow

# S7 types -> OPC UA built-in types that carry the same value
EQUIV = {
    "REAL": {"Float"}, "LREAL": {"Double"}, "BOOL": {"Boolean"},
    "INT": {"Int16"}, "UINT": {"UInt16"}, "WORD": {"UInt16"},
    "DINT": {"Int32"}, "UDINT": {"UInt32"}, "DWORD": {"UInt32"},
    "BYTE": {"Byte"}, "SINT": {"SByte"}, "USINT": {"Byte"},
    "STRING": {"String"}, "LINT": {"Int64"}, "LWORD": {"UInt64"}, "ULINT": {"UInt64"},
}


def values_match(a: Any, b: Any, rel_tol: float, abs_tol: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)
    return a == b


def age(r: Reading) -> float:
    return round((utcnow() - r.timestamp).total_seconds() * 1000, 1)


def compare_tag(s7: Reading | None, ua: Reading | None, rel_tol: float = 0.02, abs_tol: float = 0.05) -> dict:
    res: dict[str, Any] = {"tag": (s7 or ua).tag if (s7 or ua) else None,
                           "s7comm": s7.to_dict() if s7 else None, "opcua": ua.to_dict() if ua else None}
    if not s7 or not ua:
        res.update(status="INCOMPLETE", match=None, value_diff=None, skew_ms=None, type_match=None)
        return res
    usable = s7.quality is Quality.GOOD and ua.quality is Quality.GOOD
    skew = abs((s7.timestamp - ua.timestamp).total_seconds()) * 1000
    type_match = ua.datatype in EQUIV.get(s7.datatype, {ua.datatype})
    diff = None
    match = None
    if usable:
        match = values_match(s7.value, ua.value, rel_tol, abs_tol)
        if isinstance(s7.value, (int, float)) and isinstance(ua.value, (int, float)) and not isinstance(s7.value, bool):
            diff = ua.value - s7.value
    res.update(status=("MATCH" if match else "DIFFERENT") if usable else "UNUSABLE",
               match=match, value_diff=diff, skew_ms=round(skew, 1), type_match=type_match,
               freshness_ms={"s7comm": age(s7), "opcua": age(ua)})
    return res


def compare_device(acq, device_id: str, rel_tol: float = 0.02, abs_tol: float = 0.05) -> dict:
    tags = []
    for t in acq.devices[device_id].tags:
        s = acq.latest_for(device_id, t.name, "s7comm")
        u = acq.latest_for(device_id, t.name, "opcua")
        tags.append(compare_tag(s[0] if s else None, u[0] if u else None, rel_tol, abs_tol))
    now_stats = {k: acq.channel_status(c) for (d, k), c in acq.channels.items() if d == device_id}
    comparable = [t for t in tags if t["match"] is not None]
    return {
        "device_id": device_id,
        "tolerance": {"relative": rel_tol, "absolute": abs_tol},
        "note": "Independent samples: values may legitimately differ by process change between reads (see skew_ms).",
        "tags": tags,
        "summary": {
            "compared": len(comparable),
            "matching": sum(1 for t in comparable if t["match"]),
            "type_mismatches": sum(1 for t in tags if t["type_match"] is False),
            "max_skew_ms": max((t["skew_ms"] for t in tags if t["skew_ms"] is not None), default=None),
        },
        "protocol_stats": {k: {"success_rate": v["success_rate"], "latency_ms": v["latency_ms"],
                               "reads_ok": v["reads_ok"], "reads_fail": v["reads_fail"],
                               "connected": v["connected"]} for k, v in now_stats.items()},
    }
