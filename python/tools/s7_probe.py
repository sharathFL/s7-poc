"""One-shot S7 read probe: connect, read the DB7 span once with a hard timeout, print the result.
Run via `.\\run-windows.ps1 -Probe`. Read-only (db_read only)."""
import os
import sys
import threading
import time

import yaml


def main() -> int:
    cfg = yaml.safe_load(open(os.environ["CONFIG_PATH"], encoding="utf-8"))
    dev = cfg["devices"][0]
    s7 = dev["s7comm"]
    ip, rack, slot, port = dev["ip"], s7.get("rack", 0), s7.get("slot", 1), s7.get("port", 102)
    db, start, size = 7, 0, 29
    print(f"probe: {ip} rack={rack} slot={slot} port={port}  read DB{db}[{start}:{start + size}]")

    import snap7
    result: dict = {}

    def work() -> None:
        try:
            c = snap7.client.Client()
            t0 = time.perf_counter()
            c.connect(ip, rack, slot, port)
            result["connect_s"] = time.perf_counter() - t0
            t1 = time.perf_counter()
            result["data"] = bytes(c.db_read(db, start, size))
            result["read_s"] = time.perf_counter() - t1
            c.disconnect()
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(10)
    if th.is_alive():
        print("RESULT: HANG (no reply in 10 s) -> check DB7 optimized access / access rights in TIA")
        return 2
    if "error" in result:
        print("RESULT: ERROR", result["error"], f"(connect {result.get('connect_s', 0):.3f}s)")
        return 1
    d = result["data"]
    print(f"RESULT: OK  connect {result['connect_s']*1000:.0f} ms, read {result['read_s']*1000:.0f} ms")
    print("raw:", d.hex(" "))
    print("Counter_Int =", int.from_bytes(d[0:2], "big", signed=True))
    import struct
    print("Ramp_Real   =", struct.unpack(">f", d[2:6])[0])
    print("Status_String maxlen/len =", d[6], d[7], "text:", d[8:8 + d[7]].decode("latin-1", "replace"))
    print("Tick_Bool   =", bool(d[28] & 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
