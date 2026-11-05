"""Acquisition: one supervised task per (device, protocol). Persistent connections, bounded
timeouts, exponential-backoff reconnect, in-memory latest cache + bounded history."""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..config import AppConfig, DeviceConfig
from ..drivers.base import Driver
from ..drivers.factory import build_drivers
from ..models import Quality, Reading, iso, utcnow

log = logging.getLogger("acquisition")
MAX_BACKOFF_S = 30.0


@dataclass
class Channel:
    device: DeviceConfig
    driver: Driver
    kind: str                      # s7comm | opcua  (independent of mock-ness)
    connected: bool = False
    reads_ok: int = 0
    reads_fail: int = 0
    reconnects: int = 0
    last_success: datetime | None = None
    last_error: str | None = None
    latencies: deque = field(default_factory=lambda: deque(maxlen=500))
    errors: deque = field(default_factory=lambda: deque(maxlen=50))

    def record_error(self, msg: str) -> None:
        self.last_error = msg
        self.errors.append({"time": iso(utcnow()), "protocol": self.driver.protocol, "message": msg})

    def stats(self) -> dict:
        total = self.reads_ok + self.reads_fail
        lat = sorted(self.latencies)
        return {
            "reads_ok": self.reads_ok, "reads_fail": self.reads_fail,
            "success_rate": round(self.reads_ok / total, 4) if total else None,
            "latency_ms": {
                "avg": round(statistics.fmean(lat), 2) if lat else None,
                "p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 2) if lat else None,
                "max": round(lat[-1], 2) if lat else None,
            },
        }


class Acquisition:
    def __init__(self, cfg: AppConfig, drivers_for: Callable[[DeviceConfig], list[Driver]] = build_drivers):
        self.cfg = cfg
        self.devices = {d.id: d for d in cfg.devices}
        self.channels: dict[tuple[str, str], Channel] = {}
        self.latest: dict[tuple[str, str, str], Reading] = {}       # (device, kind, tag)
        self.history: dict[tuple[str, str, str], deque] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        for dev in cfg.devices:
            for drv in drivers_for(dev):
                kind = "s7comm" if "s7comm" in drv.protocol else "opcua" if "opcua" in drv.protocol else drv.protocol
                self.channels[(dev.id, kind)] = Channel(dev, drv, kind)

    # ---- lifecycle -------------------------------------------------------------------
    async def start(self) -> None:
        for ch in self.channels.values():
            self._tasks.append(asyncio.create_task(self._run(ch), name=f"{ch.device.id}:{ch.kind}"))

    async def stop(self) -> None:
        self._stopping = True
        # Python < 3.12 asyncio.wait_for can swallow a cancellation that races with completion
        # of the inner call, so re-cancel until the tasks are really gone (loops check _stopping).
        for _ in range(5):
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                break
            for t in pending:
                t.cancel()
            await asyncio.wait(pending, timeout=2)
        for ch in self.channels.values():
            await ch.driver.disconnect()

    # ---- storage ---------------------------------------------------------------------
    def _store(self, ch: Channel, r: Reading) -> None:
        key = (r.device_id, ch.kind, r.tag)
        self.latest[key] = r
        self.history.setdefault(key, deque(maxlen=self.cfg.history_size)).append(r)

    def _fail_all(self, ch: Channel, msg: str) -> None:
        now = utcnow()
        for t in ch.device.tags:
            prev = self.latest.get((ch.device.id, ch.kind, t.name))
            r = Reading(ch.device.id, ch.driver.protocol, t.name, prev.datatype if prev else t.type.upper(),
                        prev.value if prev else None, now, Quality.COMM_FAILURE, msg,
                        None, prev.metadata if prev else {})
            self._store(ch, r)

    # ---- supervised loop -------------------------------------------------------------
    async def _run(self, ch: Channel) -> None:
        drv = ch.driver
        backoff = 1.0
        subscribed = False
        sub_mode = ch.kind == "opcua" and drv.supports_subscribe and ch.device.opcua.mode == "subscribe"
        poll_s = max(0.05, drv.polling_ms / 1000)

        async def on_reading(r: Reading) -> None:
            self._store(ch, r)
            ch.last_success = utcnow()

        while not self._stopping:
            try:
                if not drv.connected:
                    await asyncio.wait_for(drv.connect(), 20)
                    ch.connected = True
                    if ch.last_error is not None or ch.reads_fail:
                        ch.reconnects += 1
                    backoff, subscribed = 1.0, False
                    log.info("[%s/%s] connected", ch.device.id, ch.kind)
                if sub_mode:
                    if not subscribed:
                        await drv.subscribe(on_reading)
                        subscribed = True
                    t0 = time.perf_counter()
                    if not await drv.healthy():
                        raise ConnectionError("keep-alive failed")
                    ch.latencies.append((time.perf_counter() - t0) * 1000)
                    ch.reads_ok += 1
                else:
                    t0 = time.perf_counter()
                    readings = await asyncio.wait_for(drv.read(), 30)
                    ch.latencies.append((time.perf_counter() - t0) * 1000)
                    for r in readings:
                        self._store(ch, r)
                    ch.reads_ok += 1
                    ch.last_success = utcnow()
                    bad = [r for r in readings if r.quality is not Quality.GOOD]
                    ch.last_error = (f"{len(bad)} item(s) invalid: " + "; ".join(f"{r.tag}: {r.error}" for r in bad[:3])) if bad else None
                    if bad:
                        ch.record_error(ch.last_error)
                await asyncio.sleep(poll_s)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any failure => reconnect cycle
                msg = f"{type(e).__name__}: {e}"[:400]
                if ch.connected or ch.reads_fail == 0 or ch.last_error != msg:
                    log.warning("[%s/%s] %s", ch.device.id, ch.kind, msg)
                ch.reads_fail += 1
                ch.connected = False
                ch.record_error(msg)
                self._fail_all(ch, msg)
                try:
                    await drv.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    # ---- queries ---------------------------------------------------------------------
    def _effective(self, ch: Channel, r: Reading) -> Reading:
        """Apply staleness at query time."""
        if r.quality is Quality.GOOD:
            limit = self.cfg.stale_after_factor * ch.driver.polling_ms / 1000
            if (utcnow() - r.timestamp).total_seconds() > max(limit, 1.0):
                from dataclasses import replace
                return replace(r, quality=Quality.STALE, error="no fresh reading")
        return r

    def latest_for(self, device_id: str, tag: str | None = None, kind: str | None = None) -> list[Reading]:
        out = []
        for (d, k, t), r in self.latest.items():
            if d == device_id and (tag is None or t == tag) and (kind is None or k == kind):
                out.append(self._effective(self.channels[(d, k)], r))
        return out

    def history_for(self, device_id: str, tag: str | None, kind: str | None, limit: int) -> list[Reading]:
        out: list[Reading] = []
        for (d, k, t), h in self.history.items():
            if d == device_id and (tag is None or t == tag) and (kind is None or k == kind):
                out.extend(h)
        out.sort(key=lambda r: r.timestamp)
        return out[-limit:]

    def channel_status(self, ch: Channel) -> dict:
        d = ch.device
        return {
            "protocol": ch.kind, "driver": ch.driver.protocol, "connected": ch.connected,
            "mock": d.mock, "polling_ms": ch.driver.polling_ms,
            "last_success": iso(ch.last_success), "last_error": ch.last_error,
            "reconnects": ch.reconnects, "recent_errors": list(ch.errors)[-10:],
            **ch.stats(),
        }

    def device_status(self, device_id: str) -> dict:
        d = self.devices[device_id]
        chans = {k: self.channel_status(c) for (dev, k), c in self.channels.items() if dev == device_id}
        n_conn = sum(1 for c in chans.values() if c["connected"])
        state = "ONLINE" if chans and n_conn == len(chans) else "DEGRADED" if n_conn else "OFFLINE"
        return {"device_id": d.id, "ip": d.ip, "mock": d.mock, "state": state, "channels": chans}
