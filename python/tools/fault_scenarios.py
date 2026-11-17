"""Scenario runner for the fault proxy: plays a fault timeline and scores every app/protocol.

For each scenario it: waits for a healthy baseline, switches the proxy through the listed steps
(e.g. blacksink for 20 s), waits for recovery, and measures per channel:
  detect_s      fault start -> first reading that is not GOOD
  recover_s     last fault end -> first stable GOOD reading
  good_but_old  GOOD readings whose own timestamp is older than max_good_age_s (stale data passed off as good)
  flaps         GOOD -> not-GOOD transitions
and gives PASS / FAIL against the thresholds in the scenario file.

Prereqs: fault_proxy.py running, the app(s) started with config\\devices.fault.yaml (they connect through
the proxy). Run:  python tools/fault_scenarios.py --scenarios ../config/fault_scenarios.yaml
Targets are the apps' REST APIs; unreachable ones are skipped. Standard library + PyYAML.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

LABELS = {("py", "s7comm"): "Snap7 (py)", ("py", "opcua"): "OPC UA (py)",
          ("go", "s7comm"): "PLC4X (go)", ("go", "opcua"): "OPC UA (go)"}


@dataclass
class Sample:
    t: float            # seconds since scenario start
    good: bool
    quality: str
    age_s: float | None  # now - reading timestamp (None if unknown)


def http_json(url: str, method: str = "GET", timeout: float = 2.0):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse_ts(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except Exception:
        return None


# ---- pure scoring (unit tested) -----------------------------------------------------------
def compute_metrics(samples: list[Sample], fault_start: float, fault_end: float, max_good_age_s: float = 4.0,
                    stable_n: int = 3) -> dict:
    base = [s for s in samples if s.t < fault_start][-8:]
    baseline_ok = bool(base) and all(s.good for s in base)
    during = [s for s in samples if fault_start <= s.t < fault_end]
    detect = next((s.t - fault_start for s in samples if s.t >= fault_start and not s.good), None)
    recover = None
    after = [s for s in samples if s.t >= fault_end]
    for i, s in enumerate(after):
        if all(x.good for x in after[i:i + stable_n]) and len(after[i:i + stable_n]) == stable_n:
            recover = s.t - fault_end
            break
    flaps = sum(1 for a, b in zip(samples, samples[1:]) if a.good and not b.good)
    old = sum(1 for s in during if s.good and s.age_s is not None and s.age_s > max_good_age_s)
    qualities: dict[str, int] = {}
    for s in during:
        qualities[s.quality] = qualities.get(s.quality, 0) + 1
    return {"baseline_ok": baseline_ok, "detect_s": None if detect is None else round(detect, 2),
            "recover_s": None if recover is None else round(recover, 2), "good_but_old": old, "flaps": flaps,
            "qualities_during_fault": qualities, "samples": len(samples)}


def verdict(m: dict, sc: dict) -> tuple[bool, list[str]]:
    why: list[str] = []
    if not m["baseline_ok"]:
        return False, ["no healthy baseline before the fault"]
    if sc.get("expect_detect", True):
        if m["detect_s"] is None:
            why.append("fault never detected")
        elif m["detect_s"] > sc["detect_within_s"]:
            why.append(f"detected after {m['detect_s']}s > {sc['detect_within_s']}s")
        if m["recover_s"] is None:
            why.append("never recovered")
        elif m["recover_s"] > sc["recover_within_s"]:
            why.append(f"recovered after {m['recover_s']}s > {sc['recover_within_s']}s")
    elif m["detect_s"] is not None:
        why.append(f"unexpected non-GOOD at +{m['detect_s']}s (this fault should be tolerated)")
    if m["good_but_old"] > 0:
        why.append(f"{m['good_but_old']} GOOD readings were older than the freshness limit")
    return (not why), why


# ---- polling ------------------------------------------------------------------------------
class Poller(threading.Thread):
    """Polls one app's /devices/{id}/readings every `period` s and files one Sample per protocol."""

    def __init__(self, impl: str, base: str, device: str, period: float = 0.25):
        super().__init__(daemon=True)
        self.impl, self.base, self.device, self.period = impl, base.rstrip("/"), device, period
        self.t0 = time.time()
        self.samples: dict[str, list[Sample]] = {}
        self._stop = threading.Event()
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.samples, self.t0 = {}, time.time()

    def run(self) -> None:
        while not self._stop.is_set():
            t = time.time() - self.t0
            try:
                rs = http_json(f"{self.base}/devices/{self.device}/readings")
                per: dict[str, list[dict]] = {}
                for r in rs:
                    per.setdefault("opcua" if "opcua" in r["protocol"] else "s7comm", []).append(r)
                batch = {}
                for proto, items in per.items():
                    bad = next((x for x in items if x["quality"] != "GOOD"), None)
                    ts = [parse_ts(x["timestamp"]) for x in items]
                    ts = [x for x in ts if x is not None]
                    age = (time.time() - min(ts)) if ts else None
                    batch[proto] = Sample(t, bad is None, bad["quality"] if bad else "GOOD", age)
            except Exception:
                batch = {p: Sample(t, False, "APP_DOWN", None) for p in (self.samples or {"s7comm": 0, "opcua": 0})}
            with self.lock:
                for p, s in batch.items():
                    self.samples.setdefault(p, []).append(s)
            self._stop.wait(self.period)

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, list[Sample]]:
        with self.lock:
            return {k: list(v) for k, v in self.samples.items()}


def proxy_mode(proxy: str, mode: str, delay_ms: int = 0) -> dict:
    return http_json(f"{proxy}/mode?mode={mode}&delay_ms={delay_ms}", "POST", timeout=15)


def run_scenario(sc: dict, proxy: str, pollers: list[Poller], log=print) -> dict:
    proxy_mode(proxy, "normal")
    for p in pollers:
        p.reset()
    log(f"\n== {sc['name']}: warmup {sc['warmup_s']}s")
    time.sleep(sc["warmup_s"])
    t_start = time.time()
    first_fault = last_fault_end = None
    events = []
    for step in sc["steps"]:
        mode, dur = step["mode"], float(step["for_s"])
        now = pollers[0].t0 and (time.time() - pollers[0].t0)
        proxy_mode(proxy, mode, int(step.get("delay_ms", 0)))
        events.append({"t": round(now, 2), "mode": mode, "for_s": dur})
        log(f"   t={now:5.1f}s  proxy -> {mode}" + (f" {step.get('delay_ms')}ms" if mode == "delay" else "") + f" for {dur}s")
        if mode != "normal" and first_fault is None:
            first_fault = now
        time.sleep(dur)
        if mode != "normal":
            last_fault_end = time.time() - pollers[0].t0
    proxy_mode(proxy, "normal")
    if first_fault is None:
        first_fault, last_fault_end = 0.0, 0.0
    log(f"   proxy -> normal, waiting up to {sc['recover_timeout_s']}s for recovery")
    deadline = time.time() + sc["recover_timeout_s"]
    while time.time() < deadline:
        time.sleep(1)
        snaps = [p.snapshot() for p in pollers]
        if all(len([s for s in ss if s.t >= last_fault_end]) >= 3 and all(s.good for s in ss[-3:])
               for sn in snaps for ss in sn.values()):
            break
    time.sleep(2)
    results = {}
    for p in pollers:
        for proto, ss in p.snapshot().items():
            m = compute_metrics(ss, first_fault, last_fault_end, sc["max_good_age_s"])
            ok, why = verdict(m, sc)
            results[f"{p.impl}/{proto}"] = {"label": LABELS.get((p.impl, proto), f"{p.impl}/{proto}"),
                                           **m, "pass": ok, "why": why}
    return {"name": sc["name"], "events": events, "fault_start": round(first_fault, 2),
            "fault_end": round(last_fault_end, 2), "results": results,
            "proxy": http_json(f"{proxy}/status"), "wall_s": round(time.time() - t_start, 1)}


def load_scenarios(path: str, only: list[str] | None) -> list[dict]:
    raw = yaml.safe_load(open(path, encoding="utf-8"))
    d = {"warmup_s": 15, "recover_timeout_s": 60, "detect_within_s": 15, "recover_within_s": 45,
         "max_good_age_s": 4.0, **(raw.get("defaults") or {})}
    out = []
    for sc in raw["scenarios"]:
        sc = {**d, **sc}
        if "steps" not in sc:
            sc["steps"] = [{"mode": sc["mode"], "for_s": sc.get("fault_s", 20), "delay_ms": sc.get("delay_ms", 0)}]
        if not only or sc["name"] in only:
            out.append(sc)
    if not out:
        raise SystemExit("no scenarios selected")
    return out


def print_table(report: list[dict]) -> bool:
    all_ok = True
    for r in report:
        print(f"\n[{r['name']}]  fault window t={r['fault_start']}s..{r['fault_end']}s")
        print(f"  {'channel':<14}{'detect':>9}{'recover':>9}{'old-GOOD':>10}{'flaps':>7}  result")
        for k, v in r["results"].items():
            f = lambda x: "-" if x is None else f"{x}s"
            print(f"  {v['label']:<14}{f(v['detect_s']):>9}{f(v['recover_s']):>9}{v['good_but_old']:>10}{v['flaps']:>7}  "
                  + ("PASS" if v["pass"] else "FAIL: " + "; ".join(v["why"])))
            all_ok &= v["pass"]
        for l in r["proxy"]["listeners"]:
            print(f"  proxy {l['listen']}: accepted={l['accepted']} up={l['up_bytes']}B down={l['down_bytes']}B dropped={l['dropped_bytes']}B")
    return all_ok


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", default=os.path.join(here, "..", "..", "config", "fault_scenarios.yaml"))
    ap.add_argument("--proxy", default="http://127.0.0.1:9000")
    ap.add_argument("--py", default="http://localhost:8001", help="Python app URL ('' to skip)")
    ap.add_argument("--go", default="http://localhost:8002", help="Go app URL ('' to skip)")
    ap.add_argument("--device", default="s7_1500_01")
    ap.add_argument("--only", action="append", help="scenario name (repeatable)")
    ap.add_argument("--report-dir", default=os.path.join(here, "..", "..", "reports"))
    a = ap.parse_args()

    try:
        http_json(f"{a.proxy}/status")
    except Exception as e:
        print(f"fault proxy not reachable at {a.proxy}: {e}\nStart it first: python tools/fault_proxy.py --plc <PLC IP>")
        return 2
    pollers: list[Poller] = []
    for impl, url in (("py", a.py), ("go", a.go)):
        if not url:
            continue
        try:
            http_json(f"{url}/health")
            pollers.append(Poller(impl, url, a.device))
            print(f"target {impl}: {url}")
        except Exception:
            print(f"target {impl}: {url} not reachable, skipped")
    if not pollers:
        print("no app reachable")
        return 2
    scenarios = load_scenarios(a.scenarios, a.only)
    for p in pollers:
        p.start()
    report = []
    try:
        for sc in scenarios:
            report.append(run_scenario(sc, a.proxy, pollers))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        try:
            proxy_mode(a.proxy, "normal")
        except Exception:
            pass
        for p in pollers:
            p.stop()
    ok = print_table(report)
    os.makedirs(a.report_dir, exist_ok=True)
    path = os.path.join(a.report_dir, f"fault-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"time": datetime.now(timezone.utc).isoformat(), "scenarios": report}, fh, indent=2)
    print(f"\nreport: {os.path.abspath(path)}\nOVERALL: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
