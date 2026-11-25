"""One test for every function-table row that has no row-named test elsewhere (document section 2.5).

The row labels V, N, K, L and A come from the docstrings of AdcCommunicationManager.receive,
AlarmManager.on_alarm_notice, AlarmManager.acknowledge and AccessControl.login and authorise.
the traceability script fails when a row of the document has no test whose name starts with its label."""
import json
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.adc_comm import sign
from monitoring.domain import AlarmState, Authorisation, Category, Fn, Role, SysMode


class RowTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()

    def last_message(self):
        return self.w.sys.history.query(category=Category.MESSAGE)[-1]

    # ---- ADC Communication Manager, rules V1 to V7
    def test_V2_unregistered_adc_is_rejected(self):
        f = {"adc_id": "ADC-77", "message_id": 1, "timestamp": self.w.clock.now(), "type": "status",
             "mode": "Normal"}
        ans = json.loads(self.w.sys.handle_adc_bytes(json.dumps({**f, "auth": sign("k", f)}).encode()))
        self.assertEqual(ans["result"], "rejected")
        self.assertEqual(self.last_message().data["reason"], "unregistered ADC")

    def test_V3_wrong_code_is_rejected_with_its_own_recorded_reason(self):
        self.assertEqual(self.adc.status(bad_auth=True)["result"], "rejected")
        self.assertEqual(self.last_message().data["reason"], "bad authentication code")

    def test_V4_missing_field_is_rejected_and_names_the_rule(self):
        ans = self.adc.send("alarm_notice", alarm_type="door open", severity="high")
        self.assertEqual(ans["result"], "rejected")
        self.assertTrue(self.last_message().data["reason"].startswith("bad format"))

    def test_V5_a_second_message_with_the_same_id_is_a_duplicate(self):
        self.adc.status()
        again = self.adc.send_fields(self.adc.build("status", mid=self.adc.mid, mode="Normal"))
        self.assertEqual(again["result"], "accepted")
        self.assertEqual(self.last_message().data["outcome"], "Duplicate")

    def test_V6_old_adc_time_is_late_and_keeps_that_time(self):
        old = self.w.clock.now() - 31
        self.assertEqual(self.adc.send("status", ts=old, mode="Normal")["result"], "accepted")
        self.assertEqual(self.last_message().data["adc_ts"], old)

    def test_V7_a_valid_current_message_is_accepted(self):
        self.assertEqual(self.adc.status()["result"], "accepted")
        self.assertEqual(self.last_message().data["outcome"], "Accepted")

    # ---- Alarm Manager, rows N1, N2 and K1 to K4
    def test_N1_a_new_alarm_key_creates_a_raised_alarm(self):
        self.adc.alarm("A-1")
        self.assertIs(self.w.sys.alarms.get("ADC-01/A-1").state, AlarmState.RAISED)

    def test_N2_a_known_alarm_key_is_ignored_and_recorded(self):
        self.adc.alarm("A-1")
        self.adc.alarm("A-1")
        self.assertEqual(sum(1 for a in self.w.sys.alarms.active_alarms() if a.alarm_id == "A-1"), 1)
        self.assertTrue(any(e.data.get("machine") == "alarm" and e.data.get("alarm_key") == "ADC-01/A-1"
                            for e in self.w.sys.history.query(kind="ignored_event")))

    def test_K1_fault_mode_refuses_acknowledgements(self):
        self.adc.alarm("A-1")
        self.w.sys.history.inject_failure(True)
        self.w.sys.history.append(Category.SYSTEM, "probe")
        self.assertIs(self.w.sys.mode.mode(), SysMode.FAULT)
        res = self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), "ADC-01/A-1")
        self.assertFalse(res.ok)
        self.assertIs(self.w.sys.alarms.get("ADC-01/A-1").state, AlarmState.RAISED)

    def test_K2_an_authorisation_for_another_function_is_refused_and_recorded(self):
        self.adc.alarm("A-1")
        res = self.w.sys.alarms.acknowledge(Authorisation("nurse1", Role.NURSE, Fn.VIEW_DASHBOARD), "ADC-01/A-1")
        self.assertFalse(res.ok)
        self.assertTrue(self.w.sys.history.query(kind="refused_event"))

    def test_K3_unknown_alarm_key_is_refused(self):
        res = self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), "ADC-01/NOPE")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "no such alarm")

    def test_K4_a_pharmacist_acknowledges_a_raised_alarm(self):
        self.adc.alarm("A-1")
        res = self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), "ADC-01/A-1")
        self.assertTrue(res.ok)
        a = self.w.sys.alarms.get("ADC-01/A-1")
        self.assertIs(a.state, AlarmState.ACKNOWLEDGED)
        self.assertEqual(a.acknowledged_by, "pharm1")

    # ---- Access Control, rows L1 and A2
    def test_L1_a_locked_account_refuses_even_the_right_password(self):
        for _ in range(5):
            self.assertFalse(self.w.sys.access.login("pharm1", "wrong-password").ok)
        res = self.w.sys.access.login("pharm1", "pharm1-password")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "account locked")

    def test_L3_four_wrong_passwords_do_not_lock_the_account(self):
        for _ in range(4):
            res = self.w.sys.access.login("pharm1", "wrong-password")
            self.assertEqual(res.reason, "wrong user ID or password")
        self.assertTrue(self.w.sys.access.login("pharm1", "pharm1-password").ok)

    def test_A2_a_valid_session_with_the_right_role_gets_an_authorisation(self):
        a = self.w.sys.access.authorise(self.w.tokens["tech1"], Fn.ACK_ALARM)
        self.assertIsNotNone(a)
        self.assertEqual((a.user_id, a.fn), ("tech1", Fn.ACK_ALARM))


if __name__ == "__main__":
    unittest.main()
