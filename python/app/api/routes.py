"""REST API. READ-ONLY by design: only GET routes exist; no route reaches a PLC write."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..models import iso, utcnow
from ..services.comparison import compare_device

router = APIRouter()


def _acq(request: Request):
    return request.app.state.acq


def _dev(request: Request, device_id: str):
    acq = _acq(request)
    if device_id not in acq.devices:
        raise HTTPException(404, f"unknown device {device_id!r}")
    return acq.devices[device_id]


def _kind(protocol: str | None) -> str | None:
    if protocol and protocol not in ("s7comm", "opcua"):
        raise HTTPException(422, "protocol must be s7comm or opcua")
    return protocol


@router.get("/health")
async def health(request: Request):
    acq = _acq(request)
    chans = list(acq.channels.values())
    return {"status": "ok", "time": iso(utcnow()), "implementation": "python",
            "devices": len(acq.devices), "channels_connected": sum(c.connected for c in chans),
            "channels_total": len(chans)}


@router.get("/devices")
async def devices(request: Request):
    acq = _acq(request)
    return [{"id": d.id, "ip": d.ip, "mock": d.mock, "tags": len(d.tags),
             "state": acq.device_status(d.id)["state"]} for d in acq.devices.values()]


@router.get("/devices/{device_id}/status")
async def status(request: Request, device_id: str):
    _dev(request, device_id)
    return _acq(request).device_status(device_id)


@router.get("/devices/{device_id}/tags")
async def tags(request: Request, device_id: str):
    d = _dev(request, device_id)
    return [{"name": t.name, "type": t.type, "s7_address": t.s7_address, "opcua_node": t.opcua_node}
            for t in d.tags]


@router.get("/devices/{device_id}/readings")
async def readings(request: Request, device_id: str, protocol: str | None = None, tag: str | None = None,
                   history: bool = False, limit: int = Query(100, ge=1, le=5000)):
    _dev(request, device_id)
    acq = _acq(request)
    k = _kind(protocol)
    rs = acq.history_for(device_id, tag, k, limit) if history else acq.latest_for(device_id, tag, k)
    return [r.to_dict() for r in rs]


@router.get("/devices/{device_id}/tags/{tag}/latest")
async def latest(request: Request, device_id: str, tag: str, protocol: str | None = None):
    d = _dev(request, device_id)
    if tag not in {t.name for t in d.tags}:
        raise HTTPException(404, f"unknown tag {tag!r}")
    rs = _acq(request).latest_for(device_id, tag, _kind(protocol))
    if not rs:
        raise HTTPException(404, "no reading yet")
    return [r.to_dict() for r in rs]


@router.get("/opcua/browse")
async def browse(request: Request, device_id: str | None = None, start: str | None = None,
                 depth: int = Query(2, ge=1, le=6)):
    acq = _acq(request)
    dev = device_id or next(iter(acq.devices))
    _dev(request, dev)
    ch = acq.channels.get((dev, "opcua"))
    if not ch:
        raise HTTPException(404, "OPC UA not enabled for this device")
    if not ch.connected:
        raise HTTPException(503, f"OPC UA not connected: {ch.last_error}")
    try:
        nodes = await ch.driver.browse(start, depth) if start or depth != 2 else await ch.driver.browse()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"browse failed: {e}") from e
    return {"device_id": dev, "start": start or "Objects", "count": len(nodes), "nodes": nodes}


@router.get("/comparison")
async def comparison(request: Request, device_id: str | None = None,
                     rel_tol: float = Query(0.02, ge=0), abs_tol: float = Query(0.05, ge=0)):
    acq = _acq(request)
    if device_id:
        _dev(request, device_id)
        return compare_device(acq, device_id, rel_tol, abs_tol)
    return [compare_device(acq, d, rel_tol, abs_tol) for d in acq.devices]
