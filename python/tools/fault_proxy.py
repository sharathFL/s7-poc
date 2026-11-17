"""TCP fault-injection proxy: sits between the apps and the PLC and misbehaves on command.

    app --> proxy (127.0.0.1:1102) --> PLC (192.168.1.2:102)

Modes (switch any time, no restart):
  normal     pass bytes through untouched
  blacksink  keep connections OPEN but silently drop all bytes both ways (half-open link: nothing
             tells the client the link is dead, so only its own timeouts can notice)
  reset      abort every connection with a TCP RST and RST new ones immediately
  refuse     stop listening: new connections get "connection refused" (and existing ones are RST)
  delay      forward everything, but hold each chunk for delay_ms first

Control API (HTTP, bound to 127.0.0.1 only; this is a test tool, never expose it):
  GET  /status
  POST /mode?mode=blacksink            (GET works too, handy for a browser or curl)
  POST /mode?mode=delay&delay_ms=2000

Run:  python tools/fault_proxy.py --plc 192.168.1.2
      python tools/fault_proxy.py --map 1102=192.168.1.2:102 --map 14840=192.168.1.2:4840 --control 9000
Standard library only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODES = ("normal", "blacksink", "reset", "refuse", "delay")


def _rst(writer: asyncio.StreamWriter | None) -> None:
    """Close with SO_LINGER(1,0) so the peer sees a TCP RST instead of an orderly FIN."""
    if writer is None:
        return
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.transport.abort()
    except Exception:
        pass


@dataclass
class Listener:
    name: str
    host: str
    port: int
    target_host: str
    target_port: int
    server: asyncio.base_events.Server | None = None
    accepted: int = 0
    up_bytes: int = 0
    down_bytes: int = 0
    dropped_bytes: int = 0
    conns: set = field(default_factory=set)  # (client_writer, upstream_writer | None)


class FaultProxy:
    def __init__(self, listeners: list[Listener]):
        self.listeners = listeners
        self.mode = "normal"
        self.delay_ms = 0
        self.mode_since = time.time()

    # ---- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        for lst in self.listeners:
            await self._listen(lst)

    async def _listen(self, lst: Listener) -> None:
        lst.server = await asyncio.start_server(lambda r, w, l=lst: self._handle(l, r, w), lst.host, lst.port)

    async def stop(self) -> None:
        for lst in self.listeners:
            self._close_listener(lst)
            self._rst_all(lst)

    @staticmethod
    def _close_listener(lst: Listener) -> None:
        if lst.server is not None:
            lst.server.close()
            lst.server = None

    @staticmethod
    def _rst_all(lst: Listener) -> None:
        for cw, uw in list(lst.conns):
            _rst(cw)
            _rst(uw)
        lst.conns.clear()

    # ---- mode switching --------------------------------------------------------------------
    async def set_mode(self, mode: str, delay_ms: int = 0) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode, self.delay_ms, self.mode_since = mode, (delay_ms if mode == "delay" else 0), time.time()
        for lst in self.listeners:
            if mode == "refuse":
                self._close_listener(lst)
                self._rst_all(lst)
            else:
                if lst.server is None:
                    await self._listen(lst)
                if mode == "reset":
                    self._rst_all(lst)

    def status(self) -> dict:
        return {"mode": self.mode, "delay_ms": self.delay_ms, "mode_for_s": round(time.time() - self.mode_since, 1),
                "listeners": [{"name": l.name, "listen": f"{l.host}:{l.port}", "target": f"{l.target_host}:{l.target_port}",
                               "listening": l.server is not None, "active": len(l.conns), "accepted": l.accepted,
                               "up_bytes": l.up_bytes, "down_bytes": l.down_bytes, "dropped_bytes": l.dropped_bytes}
                              for l in self.listeners]}

    # ---- data path -------------------------------------------------------------------------
    async def _handle(self, lst: Listener, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        lst.accepted += 1
        if self.mode == "reset":
            _rst(cwriter)
            return
        uwriter: asyncio.StreamWriter | None = None
        ureader: asyncio.StreamReader | None = None
        if self.mode != "blacksink":  # a blacksink connection never reaches the PLC
            try:
                ureader, uwriter = await asyncio.wait_for(
                    asyncio.open_connection(lst.target_host, lst.target_port), 5)
            except Exception:
                _rst(cwriter)
                return
        entry = (cwriter, uwriter)
        lst.conns.add(entry)
        tasks = [asyncio.ensure_future(self._pipe(lst, creader, uwriter, "up"))]
        if ureader is not None:
            tasks.append(asyncio.ensure_future(self._pipe(lst, ureader, cwriter, "down")))
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            lst.conns.discard(entry)
            for w in (cwriter, uwriter):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    async def _pipe(self, lst: Listener, src: asyncio.StreamReader, dst: asyncio.StreamWriter | None, way: str) -> None:
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    return
                if self.mode == "blacksink" or dst is None:
                    lst.dropped_bytes += len(data)
                    continue
                if self.mode == "delay" and self.delay_ms > 0:
                    await asyncio.sleep(self.delay_ms / 1000)
                dst.write(data)
                await dst.drain()
                if way == "up":
                    lst.up_bytes += len(data)
                else:
                    lst.down_bytes += len(data)
        except (ConnectionError, asyncio.CancelledError, OSError):
            return


# ---- control API ---------------------------------------------------------------------------
def make_control(proxy: FaultProxy, loop: asyncio.AbstractEventLoop, host: str, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, obj) -> None:
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _route(self) -> None:
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/status":
                return self._send(200, proxy.status())
            if u.path == "/mode":
                try:
                    delay = int(q.get("delay_ms", "0"))
                    asyncio.run_coroutine_threadsafe(proxy.set_mode(q.get("mode", ""), delay), loop).result(10)
                except ValueError as e:
                    return self._send(422, {"detail": str(e)})
                except Exception as e:
                    return self._send(500, {"detail": str(e)})
                return self._send(200, proxy.status())
            self._send(404, {"detail": "use /status or /mode?mode=" + "|".join(MODES)})

        do_GET = _route
        do_POST = _route

    return ThreadingHTTPServer((host, port), H)


def parse_map(s: str) -> Listener:
    """'1102=192.168.1.2:102' -> Listener"""
    try:
        lp, tgt = s.split("=", 1)
        th, tp = tgt.rsplit(":", 1)
        return Listener(name=f"{lp}->{tgt}", host="127.0.0.1", port=int(lp), target_host=th, target_port=int(tp))
    except ValueError:
        raise SystemExit(f"bad --map {s!r}: expected LISTEN_PORT=HOST:PORT")


async def amain(args: argparse.Namespace) -> None:
    maps = args.map or [f"1102={args.plc}:102", f"14840={args.plc}:4840"]
    proxy = FaultProxy([parse_map(m) for m in maps])
    await proxy.start()
    ctl = make_control(proxy, asyncio.get_running_loop(), "127.0.0.1", args.control)
    threading.Thread(target=ctl.serve_forever, daemon=True).start()
    for l in proxy.listeners:
        print(f"proxy  {l.host}:{l.port} -> {l.target_host}:{l.target_port}", flush=True)
    print(f"control http://127.0.0.1:{args.control}/status   (mode: {'|'.join(MODES)})", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        ctl.shutdown()
        await proxy.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plc", default="192.168.1.2", help="PLC IP for the default S7 (102) and OPC UA (4840) mappings")
    ap.add_argument("--map", action="append", help="LISTEN_PORT=HOST:PORT (repeatable; replaces the defaults)")
    ap.add_argument("--control", type=int, default=9000, help="control API port on 127.0.0.1")
    args = ap.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
