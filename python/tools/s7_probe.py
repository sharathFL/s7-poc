"""One-shot S7 read probe: connect, read every configured tag once (one block read per DB span) with a
hard timeout, print raw bytes and decoded values. Config-driven: run via
`.\\run-windows.ps1 -Probe` (config\\devices.yaml) or `-Config devices.s7-400.yaml -Probe`.
Uses the first device in the file. Read-only (db_read only)."""
import os
import sys
import threading
import time

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.s7types import byte_length, decode, parse_address  # noqa: E402


def plan(tags, max_span):
    """{db: [(start, size, [(name, type, addr)])]} using the same grouping as the app."""
    per_db: dict = {}
    for t in tags:
        if not t.get("s7_address"):
            continue
        a = parse_address(t["s7_address"])
        per_db.setdefault(a.db, []).append((t["name"], t["type"], a))
    out = {}
    for db, items in per_db.items():
        items.sort(key=lambda i: i[2].byte)
        groups: list = []
        for it in items:
            end = it[2].byte + byte_length(it[1])
            if groups and end - groups[-1][0][2].byte <= max_span:
                groups[-1].append(it)
            else:
                groups.append([it])
        out[db] = [(g[0][2].byte, max(i[2].byte + byte_length(i[1]) for i in g) - g[0][2].byte, g) for g in groups]
    return out


def main() -> int:
    cfg = yaml.safe_load(open(os.environ["CONFIG_PATH"], encoding="utf-8"))
    dev = cfg["devices"][0]
    s7 = dev.get("s7comm") or {}
    ip, rack, slot, port = dev["ip"], s7.get("rack", 0), s7.get("slot", 1), s7.get("port", 102)
    blocks = plan(dev["tags"], int(s7.get("max_span", 200)))
    print(f"probe: {dev['id']} {ip} rack={rack} slot={slot} port={port}")
    for db, spans in blocks.items():
        for start, size, _ in spans:
            print(f"  will read DB{db}[{start}:{start + size}] ({size} bytes)")

    import snap7
    result: dict = {"data": {}}

    def work() -> None:
        try:
            c = snap7.client.Client()
            t0 = time.perf_counter()
            c.connect(ip, rack, slot, port)
            result["connect_s"] = time.perf_counter() - t0
            for db, spans in blocks.items():
                for start, size, _ in spans:
                    t1 = time.perf_counter()
                    try:
                        result["data"][(db, start)] = (bytes(c.db_read(db, start, size)), time.perf_counter() - t1)
                    except Exception as e:  # noqa: BLE001
                        result["data"][(db, start)] = (f"{type(e).__name__}: {e}", time.perf_counter() - t1)
            c.disconnect()
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(10)
    if th.is_alive():
        print("RESULT: HANG (no reply in 10 s) -> check DB exists / is non-optimized / PLC access rights, rack+slot")
        return 2
    if "error" in result:
        print("RESULT: ERROR", result["error"], "-> connect failed: check IP, rack/slot (S7-400: 0/2 or 0/3), port 102 reachable")
        return 1
    print(f"RESULT: connected in {result['connect_s']*1000:.0f} ms")
    bad = 0
    for db, spans in blocks.items():
        for start, size, group in spans:
            buf, dt = result["data"][(db, start)]
            if isinstance(buf, str):
                bad += 1
                print(f"  DB{db}[{start}:{start + size}] FAILED: {buf}")
                continue
            print(f"  DB{db}[{start}:{start + size}] OK in {dt*1000:.1f} ms  raw: {buf.hex(' ')}")
            for name, dtype, a in group:
                off = a.byte - start
                try:
                    v = decode(dtype, buf[off:off + byte_length(dtype)], a.bit)
                except Exception as e:  # noqa: BLE001
                    v = f"<decode error: {e}>"
                print(f"    {name:<20} {dtype:<12} {a}  = {v!r}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
