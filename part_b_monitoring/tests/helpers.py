"""Shared helpers for the test suite: a system on a manual clock and a fake ADC
that builds correctly signed messages."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitoring.adc_comm import sign                       # noqa: E402
from monitoring.clock import ManualClock                   # noqa: E402
from monitoring.domain import Fn, Role                     # noqa: E402
from monitoring.policy import Policy                       # noqa: E402
from monitoring.system import MonitoringSystem             # noqa: E402


class World:
    """A started Monitoring System with accounts for every role."""

    def __init__(self, db_path=":memory:", policy=None, clock=None):
        self.clock = clock or ManualClock()
        self.sys = MonitoringSystem(db_path, self.clock, policy or Policy())
        self.sys.start()
        if not self.sys.access.has_users():
            self.sys.access.bootstrap_administrator("admin1", "admin-pass-1")
            self.tokens = {}
            self.tokens["admin1"] = self.login("admin1", "admin-pass-1")
            admin = self.auth("admin1", Fn.ADMIN)
            for uid, role in (("pharm1", Role.PHARMACIST), ("nurse1", Role.NURSE),
                              ("tech1", Role.TECHNICIAN), ("tech2", Role.TECHNICIAN)):
                assert self.sys.access.create_user(admin, uid, role, f"{uid}-password"), uid
                self.tokens[uid] = self.login(uid, f"{uid}-password")
        else:
            self.tokens = {}

    def login(self, uid, pw):
        r = self.sys.access.login(uid, pw)
        assert r.ok, r.reason
        return r.value

    def auth(self, uid, fn):
        a = self.sys.access.authorise(self.tokens[uid], fn)
        assert a is not None, f"{uid} not authorised for {fn}"
        return a

    def add_adc(self, adc_id="ADC-01", key=None, slots=((1, "Morphine", 20), (3, "Heparin", 12), (4, "Saline", 30))):
        a = self.auth("admin1", Fn.ADMIN)
        r = self.sys.config.register_adc(a, adc_id, key or f"key-{adc_id}")
        assert r.ok, r.reason
        adc = FakeAdc(self, adc_id, key or f"key-{adc_id}", list(slots))
        return adc

    def online(self, adc):
        """Drive an ADC from Offline to Online through the real recovery path."""
        adc.status()
        adc.inventory()
        return adc

    def advance(self, seconds, step=0.5):
        """Advance the clock in small steps so every timer sees its deadline."""
        t = 0.0
        while t < seconds - 1e-9:
            d = min(step, seconds - t)
            self.clock.advance(d)
            self.sys.tick()
            t += d


class FakeAdc:
    def __init__(self, world, adc_id, key, slots):
        self.w, self.id, self.key = world, adc_id, key
        self.slots = {n: [m, q] for n, m, q in slots}
        self.mid = 0
        self.out = []          # messages received from the Monitoring System

    def build(self, mtype, ts=None, mid=None, **fields):
        self.mid += 1
        f = {"adc_id": self.id, "message_id": self.mid if mid is None else mid,
             "timestamp": self.w.clock.now() if ts is None else ts, "type": mtype}
        f.update(fields)
        return f

    def raw(self, f, key=None, bad_auth=False):
        g = dict(f)
        g["auth"] = ("0" * 64) if bad_auth else sign(key or self.key, f)
        return json.dumps(g).encode()

    def send_fields(self, f, **kw):
        ans = json.loads(self.w.sys.handle_adc_bytes(self.raw(f, **kw)))
        self.out.extend(ans.get("outbox", []))
        return ans

    def send(self, mtype, ts=None, mid=None, bad_auth=False, **fields):
        return self.send_fields(self.build(mtype, ts, mid, **fields), bad_auth=bad_auth)

    # convenience senders
    def status(self, mode="Normal", **kw):
        return self.send("status", mode=mode, **kw)

    def inventory(self, **kw):
        slots = [{"slot": n, "medication": m, "quantity": q} for n, (m, q) in sorted(self.slots.items())]
        return self.send("inventory", slots=slots, **kw)

    def alarm(self, alarm_id="A-17", alarm_type="door open", severity="high", **kw):
        return self.send("alarm_notice", alarm_id=alarm_id, alarm_type=alarm_type, severity=severity, **kw)

    def clear(self, alarm_id="A-17", **kw):
        return self.send("alarm_clear", alarm_id=alarm_id, **kw)

    def request(self, request_id, request_type, slots=None, **kw):
        extra = {"slots": slots} if slots is not None else {}
        return self.send("request", request_id=request_id, request_type=request_type, **extra, **kw)

    def ack(self, config_id, slot, threshold, **kw):
        return self.send("config_ack", config_id=config_id, slot=slot, threshold=threshold, **kw)

    def got(self, mtype):
        return [m for m in self.out if m["type"] == mtype]


def make_app(system):
    """The web application as run_monitoring.py assembles it: the pages for staff (Operator Dashboard)
    with the ADC interface (ADC Transport Adapter) mounted next to them."""
    from monitoring.web.adc_gateway import create_adc_blueprint
    from monitoring.web.dashboard import create_app
    app = create_app(system)
    app.register_blueprint(create_adc_blueprint(system))
    return app
