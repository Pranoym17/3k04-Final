"""Restart: the History Store is the only persistence and every module rebuilds from it (FR-MOD-01, NFR-REL-01)."""
import os
import tempfile
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.clock import ManualClock
from monitoring.domain import AlarmState, ConnState, Fn
from monitoring.adc_comm import sign

PASSWORDS = {"admin1": "admin-pass-1", "pharm1": "pharm1-password", "nurse1": "nurse1-password",
             "tech1": "tech1-password", "tech2": "tech2-password"}


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "h.db")
        self.clock = ManualClock()
        self.w = World(self.db, clock=self.clock)
        self.adc = self.w.add_adc()
        self.w.online(self.adc)

    def restart(self):
        self.w2 = World(self.db, clock=self.clock)
        for uid, pw in PASSWORDS.items():
            self.w2.tokens[uid] = self.w2.login(uid, pw)
        a = FakeAdc(self.w2, "ADC-01", "key-ADC-01", [(1, "Morphine", 20), (3, "Heparin", 12), (4, "Saline", 30)])
        a.mid = self.adc.mid
        return self.w2, a

    def test_accounts_survive_and_passwords_still_work(self):
        w2, _ = self.restart()
        self.assertEqual(len(w2.sys.access.list_users()), 5)

    def test_FR_MOD_01_after_restart_every_adc_is_offline_with_a_communication_alarm(self):
        self.assertIs(self.w.sys.health.state_of("ADC-01"), ConnState.ONLINE)
        w2, a = self.restart()
        self.assertIs(w2.sys.health.state_of("ADC-01"), ConnState.OFFLINE)
        self.assertTrue(any(x.alarm_type == "communication" for x in w2.sys.alarms.active_alarms()))
        self.assertEqual(len(w2.sys.alarms.active_alarms()), 1)

    def test_a_valid_message_brings_the_adc_back_after_a_restart(self):
        w2, a = self.restart()
        a.status(); a.inventory()
        self.assertIs(w2.sys.health.state_of("ADC-01"), ConnState.ONLINE)
        self.assertEqual(w2.sys.alarms.active_alarms(), [])

    def test_the_adc_key_survives_a_restart(self):
        w2, a = self.restart()
        self.assertEqual(a.status()["result"], "accepted")

    def test_FR_COM_10_message_ids_seen_before_the_restart_are_still_duplicates(self):
        self.adc.alarm("A-1")
        again = self.adc.build("alarm_notice", mid=self.adc.mid, alarm_id="A-1", alarm_type="door open", severity="high")
        w2, a = self.restart()
        self.assertEqual(a.send_fields(again)["result"], "accepted")
        self.assertEqual(w2.sys.history.query(kind="adc_message")[-1].data["outcome"], "Duplicate")

    def test_alarm_states_survive(self):
        self.adc.alarm("A-1"); self.adc.alarm("A-2", severity="medium")
        self.w.sys.alarms.acknowledge(self.w.auth("tech1", Fn.ACK_ALARM), "ADC-01/A-2")
        self.w.advance(125); self.adc.status()
        self.assertIs(self.w.sys.alarms.get("ADC-01/A-1").state, AlarmState.ESCALATED)
        w2, _ = self.restart()
        self.assertIs(w2.sys.alarms.get("ADC-01/A-1").state, AlarmState.ESCALATED)
        v = w2.sys.alarms.get("ADC-01/A-2")
        self.assertEqual((v.state, v.acknowledged_by), (AlarmState.ACKNOWLEDGED, "tech1"))

    def test_cleared_alarms_stay_cleared(self):
        self.adc.alarm("A-1"); self.adc.clear("A-1")
        w2, _ = self.restart()
        self.assertIs(w2.sys.alarms.get("ADC-01/A-1").state, AlarmState.CLEARED)

    def test_confirmed_threshold_and_inventory_survive(self):
        c = self.w.sys.config.request_threshold_change(self.w.auth("pharm1", Fn.CHANGE_THRESHOLD), "ADC-01", 3, 8).value
        self.adc.ack(c.config_id, 3, 8)
        w2, _ = self.restart()
        self.assertEqual(w2.sys.inventory.confirmed_threshold("ADC-01", 3), 8)
        self.assertEqual([(s.slot, s.quantity) for s in w2.sys.inventory.slots("ADC-01")],
                         [(1, 20), (3, 12), (4, 30)])

    def test_an_unconfirmed_change_still_fails_after_sixty_seconds(self):
        self.w.sys.config.request_threshold_change(self.w.auth("pharm1", Fn.CHANGE_THRESHOLD), "ADC-01", 3, 8)
        w2, _ = self.restart()
        self.assertEqual(w2.sys.config.latest_change("ADC-01", 3).status, "Unconfirmed")
        w2.advance(61)
        self.assertEqual(w2.sys.config.latest_change("ADC-01", 3).status, "Failed")

    def test_next_config_id_does_not_repeat(self):
        c = self.w.sys.config.request_threshold_change(self.w.auth("pharm1", Fn.CHANGE_THRESHOLD), "ADC-01", 3, 8).value
        w2, a = self.restart()
        a.status(); a.inventory()
        c2 = w2.sys.config.request_threshold_change(w2.auth("pharm1", Fn.CHANGE_THRESHOLD), "ADC-01", 3, 9).value
        self.assertGreater(c2.config_id, c.config_id)

    def test_D_restart_pending_maintenance_request_is_rejected_system_restarted(self):
        self.adc.request("M-1", "maintenance")
        w2, a = self.restart()
        self.assertEqual(w2.sys.requests.pending_maintenance(), [])
        a.status()
        r = [m for m in a.got("request_reply") if m["request_id"] == "M-1"]
        self.assertEqual([(m["decision"], m["reason"]) for m in r], [("reject", "system restarted")])

    def test_answered_requests_are_not_answered_again_after_restart(self):
        self.adc.request("R-1", "refill", slots=[1])
        w2, a = self.restart()
        before = len(w2.sys.history.query(kind="request_reply"))
        a.status()
        self.assertEqual(len(w2.sys.history.query(kind="request_reply")), before)

    def test_a_locked_account_is_still_locked_after_restart(self):
        for _ in range(5):
            self.w.sys.access.login("nurse1", "wrong-password")
        w2 = World(self.db, clock=self.clock)
        self.assertEqual(w2.sys.access.login("nurse1", PASSWORDS["nurse1"]).reason, "account locked")

    def test_deregistered_adc_is_still_deregistered(self):
        self.w.sys.config.deregister_adc(self.w.auth("admin1", Fn.ADMIN), "ADC-01")
        w2, a = self.restart()
        self.assertEqual(w2.sys.config.registered_ids(), [])
        self.assertEqual(a.status()["result"], "rejected")

    def test_history_is_complete_and_in_order_after_restart(self):
        n = len(self.w.sys.history.query())
        w2, _ = self.restart()
        evs = w2.sys.history.query()
        self.assertGreaterEqual(len(evs), n)
        self.assertEqual([e.seq for e in evs], sorted(e.seq for e in evs))
        self.assertTrue(w2.sys.history.query(kind="system_start"))

    def test_restart_twice_does_not_duplicate_state(self):
        w2, _ = self.restart()
        w3 = World(self.db, clock=self.clock)
        self.assertEqual(w3.sys.config.registered_ids(), ["ADC-01"])
        self.assertEqual(len(w3.sys.access.list_users()), 5)
        self.assertEqual(len(w3.sys.alarms.active_alarms()), 1)


if __name__ == "__main__":
    unittest.main()
