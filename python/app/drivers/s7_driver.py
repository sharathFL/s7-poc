"""S7comm driver (python-snap7). READ-ONLY: the only PLC call used is db_read()."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from ..config import DeviceConfig
from ..models import Quality, Reading, utcnow
from ..s7types import AddressError, S7Address, byte_length, decode, parse_address
from .base import Driver, DriverError

log = logging.getLogger("s7driver")
MAX_SPAN = 200  # bytes; coalesce tags of one DB into one request when they fit


class S7Driver(Driver):
    protocol = "s7comm"

    def __init__(self, device: DeviceConfig):
        super().__init__(device)
        self.cfg = device.s7
        self.polling_ms = self.cfg.polling_ms
        self._client = None
        self._plan: dict[int, list[tuple[str, str, S7Address]]] = defaultdict(list)
        self._bad: dict[str, str] = {}
        for t in device.tags:
            if not t.s7_address:
                continue
            try:
                addr = parse_address(t.s7_address)
                byte_length(t.type)
                self._plan[addr.db].append((t.name, t.type, addr))
            except AddressError as e:
                self._bad[t.name] = str(e)

    def _connect_blocking(self) -> None:
        from snap7.client import Client
        from snap7.type import Parameter
        c = Client()
        c.connect(self.device.ip, self.cfg.rack, self.cfg.slot, self.cfg.port)
        for p in (Parameter.RecvTimeout, Parameter.SendTimeout, Parameter.PingTimeout):
            try:
                c.set_param(p, self.cfg.timeout_ms)
            except Exception:  # noqa: BLE001 - timeouts are best effort
                pass
        self._client = c

    async def connect(self) -> None:
        try:
            await asyncio.to_thread(self._connect_blocking)
        except Exception as e:
            self.connected = False
            raise DriverError(f"S7 connect to {self.device.ip} rack={self.cfg.rack} slot={self.cfg.slot} failed: {e}") from e
        self.connected = True
        log.info("s7comm connected to %s", self.device.ip)

    async def disconnect(self) -> None:
        c, self._client = self._client, None
        self.connected = False
        if c:
            try:
                await asyncio.to_thread(c.disconnect)
            except Exception:  # noqa: BLE001
                pass

    def _read_blocking(self) -> list[Reading]:
        c = self._client
        if c is None or not c.get_connected():
            raise DriverError("S7 not connected")
        out: list[Reading] = []
        t0 = time.perf_counter()
        for db, items in self._plan.items():
            items = sorted(items, key=lambda i: i[2].byte)
            groups: list[list] = []
            for it in items:
                end = it[2].byte + byte_length(it[1])
                if groups and end - groups[-1][0][2].byte <= MAX_SPAN:
                    groups[-1].append(it)
                else:
                    groups.append([it])
            for g in groups:
                start = g[0][2].byte
                size = max(i[2].byte + byte_length(i[1]) for i in g) - start
                try:
                    buf = c.db_read(db, start, size)
                except Exception as e:
                    raise DriverError(f"S7 read DB{db}[{start}:{start+size}] failed: {e}") from e
                ts = utcnow()
                lat = (time.perf_counter() - t0) * 1000
                for name, dtype, addr in g:
                    meta = {"address": str(addr), "db": db}
                    try:
                        off = addr.byte - start
                        val = decode(dtype, buf[off:off + byte_length(dtype)], addr.bit)
                        out.append(Reading(self.device.id, self.protocol, name, dtype.upper(), val, ts,
                                           Quality.GOOD, None, lat, meta))
                    except Exception as e:  # noqa: BLE001
                        out.append(Reading(self.device.id, self.protocol, name, dtype.upper(), None, ts,
                                           Quality.INVALID, f"decode: {e}", lat, meta))
        for name, err in self._bad.items():
            out.append(Reading(self.device.id, self.protocol, name, "?", None, utcnow(), Quality.INVALID, err))
        return out

    async def read(self) -> list[Reading]:
        return await asyncio.to_thread(self._read_blocking)
