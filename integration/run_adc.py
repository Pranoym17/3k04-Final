#!/usr/bin/env python3
"""Run Part A's ADC console connected to the Monitoring System.

    python integration/run_adc.py --url https://127.0.0.1:5000 --insecure

This reuses Part A's own command loop (adc/main.py) unchanged. It only swaps
what adc.main.build() returns, which Part A's main.py says is "a change to
these five lines and nothing else":
  * the cabinet ID is the one registered at the Monitoring System (ADC-01),
  * the clock runs in real time but `advance N` still jumps it (DemoClock),
  * every App call is serialised by one lock, because the adapter and the
    hardware poll run on their own threads,
  * the 5 s hardware poll (FR-FLT-01..03) runs on a thread, so the link
    timeout is real.
Three extra console words are handled here and never reach Part A:
  net      adapter state, last adapter log lines
  netdown  simulate a pulled cable (the adapter stops sending)
  netup    plug the cable back in
"""
import argparse
import builtins
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "part_a_adc"))
sys.path.insert(0, HERE)

import adc.main as console                                           # noqa: E402
from adc.app import AdcApplication                                   # noqa: E402
from adc.behaviour.timing_policy import TimingPolicy                 # noqa: E402
from adc.hardware.clock import Clock                                 # noqa: E402
from adc.hardware.credential_reader import SimulatedCredentialReader  # noqa: E402
from adc.hardware.drawer_sensor import SimulatedDrawerSensor         # noqa: E402
from adc.hardware.lock_driver import SimulatedLockDriver             # noqa: E402
from adc.hardware.removal_sensor import SimulatedRemovalSensor       # noqa: E402
from adc_bridge import HttpTransport, MonitoringLink                 # noqa: E402

EXTRA_HELP = """Integration (handled by integration/run_adc.py, not by Part A)
  net                         adapter state and its last log lines
  netdown / netup             pull / restore the network cable
"""


class DemoClock(Clock):
    """Real monotonic time plus a manual offset, so `advance` still works."""

    def __init__(self):
        self._start = time.monotonic()
        self._offset = 0.0

    def now_s(self):
        return time.monotonic() - self._start + self._offset

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("time cannot move backwards")
        self._offset += seconds


class LockedApp:
    """Forwards every AdcApplication call under one lock."""

    def __init__(self, app, lock):
        object.__setattr__(self, "_app", app)
        object.__setattr__(self, "_lock", lock)

    def __getattr__(self, name):
        attr = getattr(self._app, name)
        if not callable(attr):
            return attr
        def call(*a, **k):
            with self._lock:
                return attr(*a, **k)
        return call


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="https://127.0.0.1:5000")
    ap.add_argument("--adc-id", default="ADC-01")
    ap.add_argument("--key", default=None, help="default demo-key-<adc-id>, as seeded by Part B")
    ap.add_argument("--insecure", action="store_true", help="accept the self-signed lab certificate")
    ap.add_argument("--status-interval", type=float, default=10.0)
    a = ap.parse_args()

    lock = threading.RLock()
    state = {}

    def build():
        clock = DemoClock()
        locks = SimulatedLockDriver(console.DRAWERS)
        drawers = SimulatedDrawerSensor(console.DRAWERS)
        reader = SimulatedCredentialReader(console.TOKENS)
        removal = SimulatedRemovalSensor()
        app = AdcApplication(a.adc_id, clock, locks, drawers, reader, removal,
                             console.DRAWERS, console.ITEMS, console.ROLES, console.INACTIVE)
        locked = LockedApp(app, lock)
        link = MonitoringLink(locked, a.adc_id, a.key or f"demo-key-{a.adc_id}",
                              HttpTransport(a.url, insecure=a.insecure),
                              status_interval_s=a.status_interval)
        link.lock = lock
        state.update(app=locked, link=link)
        stop = threading.Event()
        state["stop"] = stop
        threading.Thread(target=link.run_forever, args=(0.5, stop), daemon=True).start()

        def hw_poll():
            while not stop.wait(TimingPolicy().heartbeat_period_s()):
                note = locked.poll_hardware()
                if note != "All devices healthy.":
                    print("\n[5 s hardware poll] " + note)
        threading.Thread(target=hw_poll, daemon=True).start()
        return locked, clock, locks, drawers, removal

    real_input = builtins.input

    def integration_input(prompt=""):
        while True:
            line = real_input(prompt)
            word = line.strip().lower()
            link = state.get("link")
            if word == "net" and link:
                print("  cable %s, Monitoring System at %s" % ("PULLED" if link.cut else "connected", a.url))
                for row in link.log[-8:]:
                    print("   ", row)
            elif word == "netdown" and link:
                link.cut = True
                print("  Network cable pulled. The ADC keeps running; messages stay in its queue.")
            elif word == "netup" and link:
                link.cut = False
                print("  Network cable restored. Stored messages will be sent oldest first.")
            elif word == "help":
                print(EXTRA_HELP)
                return line
            else:
                return line

    console.build = build
    console.input = integration_input          # adc.main looks up input() in its own module first
    print(f"ADC {a.adc_id} connected to Monitoring System at {a.url}")
    print(EXTRA_HELP)
    try:
        console.main()
    finally:
        if "stop" in state:
            state["stop"].set()


if __name__ == "__main__":
    main()
