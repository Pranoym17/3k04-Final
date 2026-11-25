#!/usr/bin/env python3
"""Launch both parts for the Deliverable 3 demonstration.

    python integration/run_demo.py                 # interactive: Monitoring System + ADC console
    python integration/run_demo.py --scripted      # rehearsal: runs both scenarios and prints evidence

Interactive mode starts the Monitoring System (Part B) on https://127.0.0.1:5000
with a fresh seeded history, then opens Part A's ADC console in this terminal,
connected as ADC-01. Open the dashboard in a browser (admin1 / 12345).

Scripted mode drives the same two programs through the end to end scenario and
the off normal scenario, reads Part B's History (read only) and prints what it
saw. Use --time-scale to shorten Part B's 30 s timers (default 3 in scripted mode).
"""
import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PART_B = os.path.join(ROOT, "part_b_monitoring")
RUNS = os.path.join(HERE, "runs")


def start_monitoring(port, scale, db):
    os.makedirs(os.path.dirname(db), exist_ok=True)
    cmd = [sys.executable, "-W", "ignore", os.path.join(PART_B, "run_monitoring.py"),
           "--fresh", "--seed", "--demo", "--db", db, "--port", str(port), "--time-scale", str(scale)]
    log = open(os.path.join(RUNS, "monitoring_stdout.log"), "w")
    proc = subprocess.Popen(cmd, cwd=PART_B, stdout=log, stderr=subprocess.STDOUT)
    end = time.time() + 30
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return proc
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    proc.kill()
    sys.exit("Monitoring System did not start; see integration/runs/monitoring_stdout.log")


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def history(db, adc="ADC-01"):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = list(con.execute("SELECT seq, ts, category, kind, data FROM events WHERE adc_id=? ORDER BY seq", (adc,)))
    con.close()
    return [(seq, ts, cat, kind, json.loads(d)) for seq, ts, cat, kind, d in rows]


def scripted(port, scale, db):
    sys.path.insert(0, os.path.join(ROOT, "part_a_adc"))
    sys.path.insert(0, HERE)
    from adc.app import AdcApplication
    from adc.hardware.credential_reader import SimulatedCredentialReader
    from adc.hardware.drawer_sensor import SimulatedDrawerSensor
    from adc.hardware.lock_driver import SimulatedLockDriver
    from adc.hardware.removal_sensor import SimulatedRemovalSensor
    from adc.main import DRAWERS, INACTIVE, ITEMS, ROLES, TOKENS
    from adc_bridge import HttpTransport, MonitoringLink
    from run_adc import DemoClock

    clock = DemoClock()
    locks, drawers, removal = SimulatedLockDriver(DRAWERS), SimulatedDrawerSensor(DRAWERS), SimulatedRemovalSensor()
    adc = AdcApplication("ADC-01", clock, locks, drawers, SimulatedCredentialReader(TOKENS), removal,
                         DRAWERS, ITEMS, ROLES, INACTIVE)
    print("ADC mode after startup:", adc.start_up().value)
    link = MonitoringLink(adc, "ADC-01", "demo-key-ADC-01",
                          HttpTransport(f"https://127.0.0.1:{port}", insecure=True),
                          status_interval_s=10.0 / scale)

    def run_for(seconds):
        end = time.time() + seconds
        while time.time() < end:
            link.pump()
            adc.poll_hardware()
            time.sleep(0.5)

    def show(title, since=0):
        print(f"\n--- Part B History for ADC-01: {title}")
        for seq, ts, cat, kind, d in history(db):
            if seq <= since:
                continue
            if cat == "message":
                extra = d.get("content") or {}
                brief = {k: extra[k] for k in list(extra)[:4]}
                print(f"  #{seq:<4} message {d['msg_type'] or '?':<13} {d['outcome']:<9} {d['reason'] or ''} {brief}")
            else:
                print(f"  #{seq:<4} {cat:<8} {kind:<22} {json.dumps(d)[:110]}")
        return max([r[0] for r in history(db)] or [0])

    def dispense(item, qty):
        adc.authenticate("card-nurse")
        print("  ADC:", adc.request_dispense(item, qty)[1].splitlines()[0])
        for d in DRAWERS:
            removal.set_confirmed(d, False)
        print("  ADC:", adc.confirm_dispense())
        for d in DRAWERS:
            removal.set_confirmed(d, True)
            drawers.set_closed(d)
        print("  ADC:", adc.close_drawer_and_complete())

    print("\n=== Scenario 1, end to end")
    run_for(2)
    print("  ADC:", adc.banner())
    dispense("MORPH-10MG", 2)
    run_for(1)
    locks.inject_feedback_fault("D2")
    print("  ADC: lock feedback on D2 jammed")
    adc.poll_hardware(); clock.advance(3); print("  ADC:", adc.poll_hardware())
    run_for(1)
    locks.clear_feedback_fault("D2")
    adc.authenticate("card-tech")
    print("  ADC:", adc.clear_fault(self_test_passes=True))
    run_for(1)
    mark = show("scenario 1")
    print("  adapter thresholds held:", link.thresholds, "| ADC queue:", adc.pending_messages())

    print("\n=== Scenario 2, off normal: cable pulled, dispense offline, replay")
    link.cut = True
    print("  cable pulled")
    wait = max(31.0 / scale, 16.0) + 1
    run_for(wait)
    print("  ADC:", adc.banner())
    dispense("PARA-500MG", 3)
    print("  ADC queue while offline:", adc.pending_messages())
    run_for(31.0 / scale + 1)
    link.cut = False
    print("  cable restored")
    run_for(2)
    print("  ADC:", adc.banner())
    show("scenario 2", mark)
    print("\nAdapter log (last 12):")
    for row in link.log[-12:]:
        print("  ", row)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--time-scale", type=float, default=None)
    ap.add_argument("--scripted", action="store_true")
    a = ap.parse_args()
    scale = a.time_scale or (3.0 if a.scripted else 1.0)
    db = os.path.join(RUNS, "demo_history.db")
    proc = start_monitoring(a.port, scale, db)
    print(f"Monitoring System on https://127.0.0.1:{a.port} (time scale {scale:g}); "
          f"accounts admin1, pharm1, nurse1, tech1 / 12345", flush=True)
    try:
        if a.scripted:
            scripted(a.port, scale, db)
        else:
            subprocess.call([sys.executable, os.path.join(HERE, "run_adc.py"), "--insecure",
                             "--url", f"https://127.0.0.1:{a.port}", "--status-interval", str(10.0 / scale)])
    finally:
        stop(proc)
        print("Monitoring System stopped.")


if __name__ == "__main__":
    main()
