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


def scan(ip: str, port: int) -> int:
    """Try rack/slot combinations and report which ones the CPU accepts. A wrong rack/slot makes most
    CPUs reset the connection during the handshake (WinError 10054), so this finds the right pair."""
    import snap7
    print(f"scan: {ip}:{port}  rack 0-1 x slot 0-5 (5 s per try, one connection at a time)")
    found = []
    for rack in (0, 1):
        for slot in (2, 3, 1, 0, 4, 5):
            res: dict = {}

            def work() -> None:
                c = snap7.client.Client()
                try:
                    c.connect(ip, rack, slot, port)
                    res["ok"] = True
                    try:
                        info = c.get_cpu_info()
                        res["cpu"] = f"{getattr(info, 'ModuleTypeName', b'')!r} {getattr(info, 'ModuleName', b'')!r}"
                    except Exception as e:  # noqa: BLE001
                        res["cpu"] = f"(cpu info failed: {e})"
                    try:
                        c.db_read(1, 0, 2)
                        res["db1"] = "DB1 readable"
                    except Exception as e:  # noqa: BLE001
                        res["db1"] = f"DB1 read failed: {e}"
                except Exception as e:  # noqa: BLE001
                    res["err"] = f"{type(e).__name__}: {e}"
                finally:
                    try:
                        c.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

            th = threading.Thread(target=work, daemon=True)
            th.start()
            th.join(5)
            if th.is_alive():
                print(f"  rack={rack} slot={slot}: HANG")
            elif res.get("ok"):
                print(f"  rack={rack} slot={slot}: CONNECTED  {res.get('cpu')}  {res.get('db1')}")
                found.append((rack, slot))
            else:
                print(f"  rack={rack} slot={slot}: {res.get('err')}")
            time.sleep(0.7)  # let the CPU free the connection resource before the next try
    if found:
        print(f"RESULT: accepted rack/slot: {found} -> set rack/slot in the config")
        return 0
    print("RESULT: no rack/slot accepted. Other causes: all CPU connection resources in use (stop other S7 clients, "
          "close TIA/STEP7 online views), access protection on the CPU, or a CP/other interface than X5 (try 192.168.2.4).")
    return 1


def main() -> int:
    cfg = yaml.safe_load(open(os.environ["CONFIG_PATH"], encoding="utf-8"))
    dev = cfg["devices"][0]
    s7 = dev.get("s7comm") or {}
    ip, rack, slot, port = dev["ip"], s7.get("rack", 0), s7.get("slot", 1), s7.get("port", 102)
    if "--scan" in sys.argv:
        return scan(ip, port)
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
