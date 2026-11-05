"""OPC UA driver (asyncua). READ-ONLY: only read / browse / subscribe are used."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from asyncua import Client, ua

from ..config import DeviceConfig
from ..models import Quality, Reading, utcnow
from .base import Driver, DriverError, ReadingCallback

log = logging.getLogger("opcuadriver")


class OpcUaDriver(Driver):
    protocol = "opcua"
    supports_subscribe = True

    def __init__(self, device: DeviceConfig):
        super().__init__(device)
        self.cfg = device.opcua
        self.polling_ms = self.cfg.polling_ms
        self._client: Client | None = None
        self._sub = None
        self._nodes: dict[str, Any] = {}
        self._tags = {t.name: t for t in device.tags if t.opcua_node}

    def _secure(self) -> bool:
        return self.cfg.security_mode.lower() != "none" and self.cfg.security_policy.lower() != "none"

    async def connect(self) -> None:
        c = Client(self.cfg.endpoint, timeout=self.cfg.timeout_ms / 1000)
        if self.cfg.username:
            c.set_user(self.cfg.username)
            c.set_password(self.cfg.password or "")
        try:
            if self._secure():
                if not (self.cfg.client_cert and self.cfg.client_key):
                    raise DriverError("secure OPC UA needs client_cert and client_key (mounted under /secrets)")
                parts = [self.cfg.security_policy, self.cfg.security_mode, self.cfg.client_cert, self.cfg.client_key]
                if self.cfg.server_cert:
                    parts.append(self.cfg.server_cert)
                await c.set_security_string(",".join(parts))
            await c.connect()
            self._nodes = {n: c.get_node(t.opcua_node) for n, t in self._tags.items()}
        except Exception as e:
            self.connected = False
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass
            # never include credentials in the message
            raise DriverError(f"OPC UA connect to {self.cfg.endpoint} failed: {type(e).__name__}: {e}") from e
        self._client = c
        self.connected = True
        log.info("opcua connected to %s (security=%s/%s)", self.cfg.endpoint, self.cfg.security_policy, self.cfg.security_mode)

    async def disconnect(self) -> None:
        c, self._client, self._sub = self._client, None, None
        self.connected = False
        if c:
            try:
                await asyncio.wait_for(c.disconnect(), 3)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _dtype(dv: ua.DataValue) -> str:
        try:
            return dv.Value.VariantType.name
        except Exception:  # noqa: BLE001
            return "?"

    def _to_reading(self, name: str, dv: ua.DataValue, latency: float | None) -> Reading:
        tag = self._tags[name]
        meta = {"node_id": tag.opcua_node,
                "status": dv.StatusCode.name if dv.StatusCode is not None else "Good",
                "source_timestamp": dv.SourceTimestamp.isoformat() if dv.SourceTimestamp else None,
                "server_timestamp": dv.ServerTimestamp.isoformat() if dv.ServerTimestamp else None}
        good = dv.StatusCode is None or dv.StatusCode.is_good()
        val = dv.Value.Value if dv.Value is not None else None
        if isinstance(val, (bytes, bytearray)) or not isinstance(val, (int, float, str, bool, type(None))):
            val = str(val)
        q = Quality.GOOD if good and val is not None else Quality.INVALID
        return Reading(self.device.id, self.protocol, name, self._dtype(dv), val, utcnow(), q,
                       None if q is Quality.GOOD else f"status {meta['status']}", latency, meta)

    async def read(self) -> list[Reading]:
        if not self._client or not self.connected:
            raise DriverError("OPC UA not connected")
        names = list(self._nodes)
        t0 = time.perf_counter()
        try:
            dvs = await asyncio.wait_for(
                self._client.uaclient.read_attributes([n.nodeid for n in self._nodes.values()],
                                                      ua.AttributeIds.Value),
                self.cfg.timeout_ms / 1000)
        except Exception as e:
            raise DriverError(f"OPC UA read failed: {type(e).__name__}: {e}") from e
        lat = (time.perf_counter() - t0) * 1000
        return [self._to_reading(n, dv, lat) for n, dv in zip(names, dvs)]

    async def healthy(self) -> bool:
        """Cheap keep-alive: read ServerStatus/State (i=2259)."""
        if not self._client or not self.connected:
            return False
        try:
            await asyncio.wait_for(self._client.get_node(ua.NodeId(2259)).read_value(), self.cfg.timeout_ms / 1000)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def subscribe(self, callback: ReadingCallback) -> None:
        if not self._client:
            raise DriverError("OPC UA not connected")
        drv = self
        node_to_name = {n.nodeid.to_string(): name for name, n in self._nodes.items()}

        class Handler:
            async def datachange_notification(self, node, val, data):
                name = node_to_name.get(node.nodeid.to_string())
                if not name:
                    return
                dv = data.monitored_item.Value
                r = drv._to_reading(name, dv, None)
                r.metadata["delivery"] = "subscription"
                res = callback(r)
                if asyncio.iscoroutine(res):
                    await res

        self._sub = await self._client.create_subscription(self.cfg.polling_ms, Handler())
        await self._sub.subscribe_data_change(list(self._nodes.values()))

    async def browse(self, start: str | None = None, depth: int = 2) -> list[dict[str, Any]]:
        """Browse the address space from `start` (default: Objects folder). Node ids are NOT assumed."""
        if not self._client or not self.connected:
            raise DriverError("OPC UA not connected")
        root = self._client.get_node(start) if start else self._client.nodes.objects
        out: list[dict[str, Any]] = []

        async def walk(node, d: int, path: str):
            for child in await node.get_children():
                try:
                    bn = await child.read_browse_name()
                    nc = await child.read_node_class()
                except Exception:  # noqa: BLE001
                    continue
                p = f"{path}/{bn.Name}"
                item = {"node_id": child.nodeid.to_string(), "browse_name": bn.Name,
                        "node_class": nc.name, "path": p}
                if nc == ua.NodeClass.Variable:
                    try:
                        item["datatype"] = (await child.read_data_type_as_variant_type()).name
                    except Exception:  # noqa: BLE001
                        pass
                out.append(item)
                if d > 1 and nc in (ua.NodeClass.Object, ua.NodeClass.Variable) and len(out) < 2000:
                    await walk(child, d - 1, p)

        await asyncio.wait_for(walk(root, max(1, min(depth, 6)), ""), 30)
        return out
