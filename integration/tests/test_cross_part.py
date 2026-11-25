"""Cross part tests: Part A (real AdcApplication) <-> adapter <-> Part B (real MonitoringSystem).

Part B runs in process on a ManualClock behind its real HTTP routes (M15 through a
Flask test client), so every byte goes through M15 and M10 exactly as in the lab.
Part A runs on its SimulatedClock with its simulated hardware. The adapter's wall
clock is Part B's ManualClock, so 30 s timeouts take milliseconds.

Run from the repo root:  python -W ignore -m unittest discover -s integration/tests -t integration
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.join(ROOT, "part_a_adc"), os.path.join(ROOT, "part_b_monitoring"),
          os.path.join(ROOT, "integration")):
    if p not in sys.path:
        sys.path.insert(0, p)

from flask import Flask                                              # noqa: E402

from adc.app import AdcApplication                                   # noqa: E402
from adc.hardware.clock import SimulatedClock                        # noqa: E402
from adc.hardware.credential_reader import SimulatedCredentialReader  # noqa: E402
from adc.hardware.drawer_sensor import SimulatedDrawerSensor         # noqa: E402
from adc.hardware.lock_driver import SimulatedLockDriver             # noqa: E402
from adc.hardware.removal_sensor import SimulatedRemovalSensor       # noqa: E402
from adc.main import DRAWERS, INACTIVE, ITEMS, ROLES, TOKENS         # noqa: E402
from adc_bridge import LinkDown, MonitoringLink, sign                # noqa: E402
from monitoring.clock import ManualClock                             # noqa: E402
from monitoring.domain import Category, ConnState, Fn, Severity      # noqa: E402
from monitoring.policy import Policy                                 # noqa: E402
from monitoring.seed import demo_key, seed                           # noqa: E402
from monitoring.system import MonitoringSystem                       # noqa: E402
from monitoring.web.adc_gateway import create_adc_blueprint          # noqa: E402

ADC = "ADC-01"


class FlaskTransport:
    """Part B's real HTTP routes through Flask's test client. `down` simulates a cut cable;
    `lose_answer` delivers the request but loses the reply."""

    def __init__(self, system):
        app = Flask("cross_part")
        app.register_blueprint(create_adc_blueprint(system))
        self.client = app.test_client()
        self.down = False
        self.lose_answer = False
        self.posts = 0

    def post(self, raw):
        if self.down:
            raise LinkDown("cut")
        r = self.client.post("/adc/messages", data=raw)
        self.posts += 1
        if self.lose_answer:
            self.lose_answer = False
            raise LinkDown("answer lost")
        return r.get_json()

    def get_outbox(self, adc_id, ts, auth):
        if self.down:
            raise LinkDown("cut")
        r = self.client.get("/adc/outbox", query_string={"adc_id": adc_id, "ts": ts, "auth": auth})
        return r.get_json() if r.status_code == 200 else {"messages": []}


class Rig:
    """Both parts wired together through the adapter."""

    def __init__(self, key=None):
        self.bclock = ManualClock()
        self.mon = MonitoringSystem(":memory:", self.bclock, Policy())
        self.mon.start()
        seed(self.mon)                                   # registers ADC-01 with demo-key-ADC-01
        self.aclock = SimulatedClock()
        self.lock = SimulatedLockDriver(DRAWERS)
        self.drawers = SimulatedDrawerSensor(DRAWERS)
        self.removal = SimulatedRemovalSensor()
        self.adc = AdcApplication(ADC, self.aclock, self.lock, self.drawers,
                                  SimulatedCredentialReader(TOKENS), self.removal,
                                  DRAWERS, ITEMS, ROLES, INACTIVE)
        self.adc.start_up()
        self.net = FlaskTransport(self.mon)
        self.link = MonitoringLink(self.adc, ADC, key or demo_key(ADC), self.net,
                                   wall_clock=self.bclock.now)

    # time moves on both sides; Part B ticks in small steps so every timer sees its deadline
    def advance(self, seconds, pump=True, step=0.5):
        t = 0.0
        while t < seconds - 1e-9:
            d = min(step, seconds - t)
            self.bclock.advance(d)
            self.aclock.advance(d)
            with self.mon.lock:
                self.mon.tick()
            if pump:
                self.link.pump()
            t += d

    def health(self):
        with self.mon.lock:
            return self.mon.health.snapshot(ADC)

    def messages(self):
        with self.mon.lock:
            return [r for r, _ in self.mon.history.replay()
                    if r.category is Category.MESSAGE and r.adc_id == ADC]

    def dispense(self, item="MORPH-10MG", qty=2):
        self.adc.authenticate("card-nurse")
        tx, _ = self.adc.request_dispense(item, qty)
        for d in DRAWERS:
            self.removal.set_confirmed(d, False)
        self.adc.confirm_dispense()
        for d in DRAWERS:
            self.removal.set_confirmed(d, True)
            self.drawers.set_closed(d)
        return tx, self.adc.close_drawer_and_complete()

    def slot(self, n):
        with self.mon.lock:
            return {s.slot: s for s in self.mon.inventory.slots(ADC)}.get(n)


class CrossPartTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.assertEqual(self.r.link.pump(), "up")

    def test_X00_first_contact_brings_the_adc_online_with_its_inventory(self):
        self.assertIs(self.r.health().state, ConnState.ONLINE)
        self.assertEqual(self.r.adc.pending_messages(), [])         # startup MODE_CHANGE delivered and acked
        s1 = self.r.slot(1)
        self.assertEqual((s1.medication, s1.quantity), ("MORPH-10MG", 12))
        self.assertEqual(self.r.health().adc_mode.value, "Normal")

    def test_X01_adc_alarm_is_received_and_shown_by_monitoring(self):
        # (a) critical lock fault on D2 -> Part A ALARM + MODE_CHANGE FAULT
        self.r.lock.inject_feedback_fault("D2")
        self.r.adc.poll_hardware()
        self.r.aclock.advance(3)
        self.r.adc.poll_hardware()
        self.assertEqual(self.r.link.pump(), "up")
        with self.r.mon.lock:
            alarms = [a for a in self.r.mon.alarms.active_alarms() if a.adc_id == ADC]
        self.assertTrue(any(a.alarm_type == "LOCK_MISMATCH" and a.severity is Severity.HIGH for a in alarms), alarms)
        self.assertEqual(self.r.health().adc_mode.value, "Fault")
        self.assertEqual(self.r.adc.pending_messages(), [])
        # technician clears the fault on the ADC -> Part B gets alarm_clear and mode Normal
        self.r.lock.clear_feedback_fault("D2")
        self.r.adc.authenticate("card-tech")
        self.assertIn("NORMAL", self.r.adc.clear_fault(self_test_passes=True))
        self.r.link.pump()
        with self.r.mon.lock:
            still = [a for a in self.r.mon.alarms.active_alarms()
                     if a.adc_id == ADC and a.alarm_type == "LOCK_MISMATCH"]
        self.assertEqual(still, [])
        self.assertEqual(self.r.health().adc_mode.value, "Normal")

    def test_X02_dispense_appears_on_the_monitoring_side(self):
        # (b) one completed dispense -> dispensing event and inventory 12 -> 10
        tx, out = self.r.dispense("MORPH-10MG", 2)
        self.assertIn("COMPLETED", out)
        self.r.link.pump()
        msgs = self.r.messages()
        disp = [m for m in msgs if m.data["msg_type"] == "dispensing"]
        self.assertEqual(len(disp), 1)
        self.assertEqual(disp[0].data["outcome"], "Accepted")
        self.assertEqual(disp[0].data["content"]["adc_user_id"], "u-nurse-01")
        self.assertEqual(disp[0].data["content"]["quantity"], 2)
        self.assertEqual(self.r.slot(1).quantity, 10)
        self.assertEqual(self.r.adc.pending_messages(), [])

    def test_X03_uncertain_dispense_raises_a_monitoring_alarm_and_stock_does_not_move(self):
        self.r.adc.authenticate("card-nurse")
        self.r.adc.request_dispense("INSUL-100U", 2)
        self.r.adc.confirm_dispense()
        for d in DRAWERS:
            self.r.drawers.set_closed(d)                  # closed, removal never confirmed: row R5
        self.assertIn("UNCERTAIN", self.r.adc.close_drawer_and_complete())
        self.r.link.pump()
        with self.r.mon.lock:
            alarms = [a.alarm_type for a in self.r.mon.alarms.active_alarms() if a.adc_id == ADC]
        self.assertTrue(any(t.startswith("dispense UNCERTAIN") for t in alarms), alarms)
        self.assertEqual(self.r.slot(3).quantity, 8)

    def test_X04_threshold_change_reaches_the_adc_and_is_acknowledged(self):
        # (c) pharmacist changes slot 1 threshold on Part B -> configuration -> Part A -> config_ack
        with self.r.mon.lock:
            tok = self.r.mon.access.login("pharm1", "12345").value
            res = self.r.mon.config.request_threshold_change(
                self.r.mon.access.authorise(tok, Fn.CHANGE_THRESHOLD), ADC, 1, 3)
        self.assertTrue(res.ok, res.reason)
        with self.r.mon.lock:
            self.assertEqual(self.r.mon.config.changes(ADC)[-1].status, "Unconfirmed")
        self.r.link.pump()                                 # outbox poll -> configuration -> config_ack
        with self.r.mon.lock:
            self.assertEqual(self.r.mon.config.changes(ADC)[-1].status, "Confirmed")
            self.assertEqual(self.r.mon.inventory.confirmed_threshold(ADC, 1), 3)
        self.assertEqual(self.r.link.thresholds, {1: 3})

    def test_X05_link_drop_offline_stale_adc_keeps_working_then_recovers(self):
        # (d) cable cut for 31 s
        self.r.net.down = True
        self.r.advance(31)
        snap = self.r.health()
        self.assertIs(snap.state, ConnState.OFFLINE)
        self.assertTrue(snap.stale)
        # Part A declared its own link down after 15 s and still dispenses locally (A-05)
        self.r.adc.poll_hardware()
        self.assertIn("LINK: OFFLINE", self.r.adc.banner())
        tx, out = self.r.dispense("MORPH-10MG", 1)
        self.assertIn("COMPLETED", out)
        self.assertEqual(self.r.adc.inventory_snapshot()["MORPH-10MG"][1], 11)
        self.r.link.pump()
        self.assertGreaterEqual(len(self.r.adc.pending_messages()), 2)   # DISPENSE_RESULT + INVENTORY_UPDATE held
        self.r.advance(5)                                                # those messages age past nothing yet
        # cable back
        self.r.net.down = False
        self.assertEqual(self.r.link.pump(), "up")
        self.assertEqual(self.r.adc.pending_messages(), [])
        self.assertIn("LINK: ONLINE", self.r.adc.banner())
        snap = self.r.health()
        self.assertIs(snap.state, ConnState.ONLINE)
        self.assertFalse(snap.stale)
        self.assertEqual(self.r.slot(1).quantity, 11)

    def test_X06_message_replayed_after_reconnect_is_recorded_late(self):
        # (e) dispense while cut, wait more than 30 s, reconnect
        self.r.net.down = True
        tx, out = self.r.dispense("PARA-500MG", 3)
        self.r.link.pump()                                 # translated and stamped now, not sent
        self.r.advance(40)
        self.r.net.down = False
        self.r.link.pump()
        msgs = self.r.messages()
        disp = [m for m in msgs if m.data["msg_type"] == "dispensing"]
        self.assertEqual(len(disp), 1)
        self.assertEqual(disp[0].data["outcome"], "Late")
        self.assertGreater(disp[0].data["receive_ts"] - disp[0].data["adc_ts"], 30)
        # the fresh status after the replay is what brought the ADC back online
        after = [m for m in msgs if m.seq > disp[0].seq and m.data["msg_type"] == "status"]
        self.assertEqual(after[0].data["outcome"], "Accepted")
        self.assertIs(self.r.health().state, ConnState.ONLINE)

    def test_X07_message_with_wrong_key_is_rejected_and_kept_in_the_adc_queue(self):
        # (f) an ADC configured with the wrong key
        bad = Rig(key="not-the-registered-key")
        self.assertEqual(bad.link.pump(), "held")
        msgs = bad.messages()
        self.assertTrue(msgs)
        self.assertTrue(all(m.data["outcome"] == "Rejected" for m in msgs))
        self.assertEqual(msgs[0].data["reason"], "bad authentication code")
        self.assertEqual(len(bad.adc.pending_messages()), 1)       # startup MODE_CHANGE not lost
        self.assertIsNot(bad.health().state, ConnState.ONLINE)

    def test_X08_tampered_outbound_message_is_ignored_by_the_adc(self):
        forged = {"adc_id": ADC, "message_id": 99, "timestamp": self.r.bclock.now(),
                  "type": "configuration", "config_id": 7, "slot": 1, "threshold": 0}
        forged["auth"] = sign("wrong-key", {k: v for k, v in forged.items()})
        self.r.link._pending_own = []
        self.r.link._handle_outbound(forged)
        self.assertEqual(self.r.link.thresholds, {})
        self.assertIn("IGNORED", self.r.link.log[-1])

    def test_X09_lost_answer_retry_is_a_duplicate_not_a_second_dispense(self):
        tx, out = self.r.dispense("MORPH-10MG", 1)
        self.r.net.lose_answer = True                      # Part B records it, the ADC never hears back
        self.assertEqual(self.r.link.pump(), "down")
        self.assertEqual(len(self.r.adc.pending_messages()), 2)
        self.r.link.pump()
        msgs = self.r.messages()
        disp = [m for m in msgs if m.data["msg_type"] == "dispensing"]
        self.assertEqual([m.data["outcome"] for m in disp], ["Accepted", "Duplicate"])
        self.assertEqual(self.r.slot(1).quantity, 11)
        self.assertEqual(self.r.adc.pending_messages(), [])

    def test_X10_inventory_request_from_monitoring_is_answered(self):
        from monitoring.domain import OutInventoryRequest
        with self.r.mon.lock:
            self.r.mon.comm.send(ADC, OutInventoryRequest())
        before = len([m for m in self.r.messages() if m.data["msg_type"] == "inventory"])
        self.r.link.pump()
        after = len([m for m in self.r.messages() if m.data["msg_type"] == "inventory"])
        self.assertEqual(after, before + 1)

    def test_X12_adc_link_returns_online_after_an_outage_with_nothing_queued(self):
        # Part A goes OFFLINE locally but has nothing to replay; the adapter's
        # heartbeat answers must still bring Part A's own link back to ONLINE.
        self.r.net.down = True
        self.r.advance(20)
        self.r.adc.poll_hardware()
        self.assertIn("LINK: OFFLINE", self.r.adc.banner())
        self.assertEqual(self.r.adc.pending_messages(), [])
        self.r.net.down = False
        self.r.advance(1)
        self.r.adc.poll_hardware()
        self.assertIn("LINK: ONLINE", self.r.adc.banner())

    def test_X13_adc_restarted_within_a_second_does_not_reuse_message_ids(self):
        # Regression for problem 8: a second ADC process (new AdcApplication, new
        # adapter, Part A IDs back to MSG-00001) 0.3 s later must not be Duplicate.
        self.r.dispense("MORPH-10MG", 1)
        self.r.link.pump()
        self.r.bclock.advance(0.3)
        r2 = self.r
        r2.adc = AdcApplication(ADC, r2.aclock, r2.lock, r2.drawers, SimulatedCredentialReader(TOKENS),
                                r2.removal, DRAWERS, ITEMS, ROLES, INACTIVE)
        r2.adc.start_up()
        r2.link = MonitoringLink(r2.adc, ADC, demo_key(ADC), r2.net, wall_clock=r2.bclock.now)
        r2.dispense("PARA-500MG", 1)
        self.assertEqual(r2.link.pump(), "up")
        outcomes = [(m.data["msg_type"], m.data["outcome"]) for m in r2.messages()]
        self.assertNotIn("Duplicate", [o for _, o in outcomes], outcomes)
        self.assertEqual([o for t, o in outcomes if t == "dispensing"], ["Accepted", "Accepted"])

    def test_X11_threshold_change_refused_while_adc_offline(self):
        self.r.net.down = True
        self.r.advance(31)
        with self.r.mon.lock:
            tok = self.r.mon.access.login("pharm1", "12345").value
            res = self.r.mon.config.request_threshold_change(
                self.r.mon.access.authorise(tok, Fn.CHANGE_THRESHOLD), ADC, 1, 3)
        self.assertFalse(res.ok)
        self.assertIn("Offline", res.reason)


if __name__ == "__main__":
    unittest.main()
