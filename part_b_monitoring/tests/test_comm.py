"""ADC Communication Manager: validation function table V1..V7 and the external interface."""
import json
import time
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.adc_comm import sign
from monitoring.domain import Category, Fn


class CommTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()

    def outcomes(self, adc="ADC-01"):
        return [e.data["outcome"] for e in self.w.sys.history.query(category=Category.MESSAGE)]

    # ---- V1 malformed
    def test_V1_not_json_is_rejected_and_recorded(self):
        ans = json.loads(self.w.sys.handle_adc_bytes(b"not json"))
        self.assertEqual(ans["result"], "rejected")
        self.assertEqual(self.outcomes()[-1], "Rejected")

    def test_V1_json_that_is_not_an_object_or_has_no_auth(self):
        for raw in (b"[1,2]", json.dumps({"adc_id": "ADC-01"}).encode(),
                    json.dumps({"adc_id": "ADC-01", "auth": "zz"}).encode()):
            self.assertEqual(json.loads(self.w.sys.handle_adc_bytes(raw))["result"], "rejected")

    # ---- V2 unregistered
    def test_FR_COM_03_unregistered_adc_is_rejected_recorded_and_not_shown(self):
        f = {"adc_id": "ADC-99", "message_id": 1, "timestamp": self.w.clock.now(), "type": "status", "mode": "Normal"}
        ans = json.loads(self.w.sys.handle_adc_bytes(json.dumps({**f, "auth": sign("x", f)}).encode()))
        self.assertEqual(ans["result"], "rejected")
        rec = self.w.sys.history.query(category=Category.MESSAGE)[-1]
        self.assertEqual(rec.data["reason"], "unregistered ADC")
        self.assertIsNone(rec.adc_id)                       # MessageRecord points to at most one ADC
        self.assertEqual(rec.data["claimed_adc_id"], "ADC-99")
        self.assertNotIn("ADC-99", self.w.sys.config.registered_ids())

    def test_FR_COM_12_deregistered_adc_is_rejected_and_events_stay_available(self):
        self.w.online(self.adc)
        self.w.sys.config.deregister_adc(self.w.auth("admin1", Fn.ADMIN), "ADC-01")
        self.assertEqual(self.adc.status()["result"], "rejected")
        self.assertGreater(len(self.w.sys.history.query(adc_id="ADC-01")), 3)

    # ---- V3 authentication
    def test_NFR_SEC_03_wrong_auth_code_is_rejected(self):
        ans = self.adc.status(bad_auth=True)
        self.assertEqual(ans["result"], "rejected")
        self.assertEqual(self.w.sys.history.query(category=Category.MESSAGE)[-1].data["reason"],
                         "bad authentication code")

    def test_NFR_SEC_03_key_of_another_adc_is_rejected(self):
        other = self.w.add_adc("ADC-02")
        f = self.adc.build("status", mode="Normal")
        self.assertEqual(self.adc.send_fields(f, key="key-ADC-02")["result"], "rejected")

    def test_answer_does_not_reveal_whether_the_adc_id_exists(self):
        a = json.loads(self.w.sys.handle_adc_bytes(self.adc.raw(self.adc.build("status", mode="Normal"), bad_auth=True)))
        f = {"adc_id": "ADC-77", "message_id": 1, "timestamp": 1.0, "type": "status", "mode": "Normal"}
        b = json.loads(self.w.sys.handle_adc_bytes(json.dumps({**f, "auth": "0" * 64}).encode()))
        self.assertEqual(a, b)

    # ---- V4 format
    def test_FR_COM_04_alarm_notice_without_alarm_id_is_rejected(self):
        ans = self.adc.send("alarm_notice", alarm_type="door open", severity="high")
        self.assertEqual(ans["result"], "rejected")
        self.assertEqual(self.w.sys.alarms.active_count("ADC-01"), 1)   # only the communication alarm

    def test_FR_COM_04_bad_values_are_rejected(self):
        for kw in ({"type": "status", "mode": "Broken"},
                   {"type": "alarm_notice", "alarm_id": "A-1", "alarm_type": "x", "severity": "urgent"},
                   {"type": "dispensing", "slot": 1, "medication": "M", "quantity": 0, "adc_user_id": "u"},
                   {"type": "inventory", "slots": []},
                   {"type": "inventory", "slots": [{"slot": 1, "medication": "M", "quantity": -1}]},
                   {"type": "request", "request_id": "R", "request_type": "refill"},
                   {"type": "unknown_type"}):
            kw = dict(kw)
            t = kw.pop("type")
            self.assertEqual(self.adc.send(t, **kw)["result"], "rejected", kw)

    def test_NFR_SEC_02_a_patient_identifier_field_is_rejected_and_never_stored(self):
        ans = self.adc.send("dispensing", slot=1, medication="Morphine", quantity=1, adc_user_id="RN-7",
                            patient_id="P-1234")
        self.assertEqual(ans["result"], "rejected")
        blob = json.dumps([e.data for e in self.w.sys.history.query()])
        self.assertNotIn("P-1234", blob)                    # the value is never stored
        rec = self.w.sys.history.query(category=Category.MESSAGE)[-1]
        self.assertEqual(rec.data["content"], {})           # no part of a rejected body is kept

    # ---- V5 duplicate
    def test_FR_COM_10_duplicate_message_id_is_discarded_and_recorded(self):
        self.adc.alarm("A-5")
        again = self.adc.send_fields(self.adc.build("alarm_notice", mid=self.adc.mid, alarm_id="A-5",
                                                    alarm_type="door open", severity="high"))
        self.assertEqual(again["result"], "accepted")
        self.assertIn("Duplicate", self.outcomes())
        self.assertEqual(sum(1 for a in self.w.sys.alarms.active_alarms() if a.alarm_id == "A-5"), 1)

    def test_FR_COM_10_a_rejected_message_does_not_use_up_its_id(self):
        self.adc.mid = 40
        self.adc.status(bad_auth=True)                      # id 41 rejected
        self.assertEqual(self.adc.send_fields(self.adc.build("status", mid=41, mode="Normal"))["result"], "accepted")

    # ---- V6 late
    def test_FR_COM_09_replayed_message_is_recorded_late_with_original_adc_time(self):
        old = self.w.clock.now() - 120
        ans = self.adc.send("status", ts=old, mode="Normal")
        self.assertEqual(ans["result"], "accepted")
        rec = self.w.sys.history.query(category=Category.MESSAGE)[-1]
        self.assertEqual(rec.data["adc_ts"], old)
        self.assertTrue(rec.data["late"])
        self.assertEqual(rec.data["outcome"], "Late")

    def test_message_at_exactly_late_limit_is_not_late(self):
        ts = self.w.clock.now() - self.w.sys.policy.late_limit_s
        self.assertEqual(self.adc.send("status", ts=ts, mode="Normal")["result"], "accepted")

    # ---- V7 accepted + FR-HIS-01
    def test_FR_HIS_01_every_outcome_is_recorded_with_ids_and_times(self):
        self.adc.status()
        self.adc.status(bad_auth=True)
        self.adc.send("status", ts=self.w.clock.now() - 100, mode="Normal")
        self.adc.send_fields(self.adc.build("status", mid=1, mode="Normal"))      # duplicate of id 1
        self.assertEqual(sorted(set(self.outcomes())), ["Accepted", "Duplicate", "Late", "Rejected"])
        for e in self.w.sys.history.query(category=Category.MESSAGE):
            self.assertIn("receive_ts", e.data)
            self.assertIn("message_id", e.data)
            self.assertIn("adc_ts", e.data)

    # ---- outbound
    def test_FR_COM_05_configuration_goes_only_to_the_owning_adc(self):
        other = self.w.add_adc("ADC-02")
        self.w.online(self.adc), self.w.online(other)
        self.w.sys.config.request_threshold_change(self.w.auth("pharm1", Fn.CHANGE_THRESHOLD), "ADC-01", 3, 8)
        self.adc.status(), other.status()
        self.assertEqual(len(self.adc.got("configuration")), 1)
        self.assertEqual(len(other.got("configuration")), 0)

    def test_outbound_messages_are_authenticated_with_the_adcs_key(self):
        self.adc.status()
        m = dict(self.adc.got("inventory_request")[0])
        auth = m.pop("auth")
        self.assertEqual(sign("key-ADC-01", m), auth)

    def test_poll_needs_a_valid_signature_and_a_recent_time(self):
        import hashlib, hmac
        ts = f"{self.w.clock.now():.3f}"
        good = hmac.new(b"key-ADC-01", f"outbox|ADC-01|{ts}".encode(), hashlib.sha256).hexdigest()
        self.assertIn("messages", json.loads(self.w.sys.poll_outbox("ADC-01", ts, good)))
        self.assertIn("error", json.loads(self.w.sys.poll_outbox("ADC-01", ts, "0" * 64)))
        old = f"{self.w.clock.now() - 1000:.3f}"
        sig = hmac.new(b"key-ADC-01", f"outbox|ADC-01|{old}".encode(), hashlib.sha256).hexdigest()
        self.assertIn("error", json.loads(self.w.sys.poll_outbox("ADC-01", old, sig)))

    # ---- capacity and speed
    def test_NFR_PRF_03_fifty_adcs_send_in_one_second_and_nothing_is_lost(self):
        adcs = [self.w.add_adc(f"ADC-{i:02d}") for i in range(2, 51)] + [self.adc]
        self.assertEqual(len(adcs), 50)
        before = len(self.w.sys.history.query(category=Category.MESSAGE))
        t0 = time.time()
        for a in adcs:
            self.assertEqual(a.status()["result"], "accepted")
            self.assertEqual(a.inventory()["result"], "accepted")
        elapsed = time.time() - t0
        after = len(self.w.sys.history.query(category=Category.MESSAGE))
        self.assertEqual(after - before, 100)
        self.assertLess(elapsed, 5.0)                       # NFR-PRF-01 budget is 5 s

    def test_NFR_PRF_03_fifty_first_adc_is_refused_by_registration(self):
        for i in range(2, 51):
            self.w.add_adc(f"ADC-{i:02d}")
        r = self.w.sys.config.register_adc(self.w.auth("admin1", Fn.ADMIN), "ADC-51")
        self.assertFalse(r.ok)
        self.assertIn("capacity", r.reason)

    def test_handler_failure_is_recorded_and_does_not_stop_other_handlers(self):
        calls = []
        def boom(m): raise RuntimeError("boom")
        from monitoring.domain import MsgKind
        self.w.sys.comm._handlers[MsgKind.STATUS].insert(0, boom)
        self.w.sys.comm._handlers[MsgKind.STATUS].append(lambda m: calls.append(m))
        self.assertEqual(self.adc.status()["result"], "accepted")
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.w.sys.history.query(kind="handler_error"))


if __name__ == "__main__":
    unittest.main()
