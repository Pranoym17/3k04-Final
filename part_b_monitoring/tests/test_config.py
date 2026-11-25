"""Configuration Manager: registry rows R1..R6, threshold rows T1..T8, acknowledgement rows C1..C4, FR-COM-*."""
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.domain import (AlarmState, ChangeKind, Fn, Role, Severity, SysMode)
from monitoring.domain import Authorisation


def threshold_auth(w, uid="pharm1"):
    return w.auth(uid, Fn.CHANGE_THRESHOLD)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.cfg = self.w.sys.config
        self.admin = self.w.auth("admin1", Fn.ADMIN)

    def test_R1_registration_is_refused_in_maintenance_fault_and_starting(self):
        for mode in (SysMode.MAINTENANCE,):
            self.w.sys.mode.switch_to_maintenance(self.admin)
            self.assertFalse(self.cfg.register_adc(self.admin, "ADC-05").ok)
            self.w.sys.mode.switch_to_normal(self.admin)
        self.w.sys.history.inject_failure(True)
        self.w.sys.history.append(__import__("monitoring.domain", fromlist=["Category"]).Category.SYSTEM, "p", {})
        self.assertFalse(self.cfg.register_adc(self.admin, "ADC-05").ok)

    def test_R2_only_an_administrator_authorisation_registers(self):
        self.assertFalse(self.cfg.register_adc(Authorisation("pharm1", Role.PHARMACIST, Fn.CHANGE_THRESHOLD),
                                               "ADC-05").ok)

    def test_R3_adc_id_must_be_one_to_thirty_two_safe_characters(self):
        for bad in ("", "a b", "x" * 33, "ADC/1", "../x", None, "ADC\n1", "é"):
            self.assertFalse(self.cfg.register_adc(self.admin, bad).ok, repr(bad))
        self.assertTrue(self.cfg.register_adc(self.admin, "x" * 32).ok)

    def test_R4_second_registration_of_the_same_id_is_refused(self):
        self.assertTrue(self.cfg.register_adc(self.admin, "ADC-05").ok)
        self.assertFalse(self.cfg.register_adc(self.admin, "ADC-05").ok)

    def test_R5_capacity_is_fifty(self):
        for i in range(50):
            self.assertTrue(self.cfg.register_adc(self.admin, f"A{i}").ok)
        r = self.cfg.register_adc(self.admin, "A50")
        self.assertFalse(r.ok)

    def test_R6_registration_returns_the_key_once_and_history_never_shows_it(self):
        r = self.cfg.register_adc(self.admin, "ADC-05")
        self.assertTrue(r.ok)
        self.assertGreaterEqual(len(r.value), 32)
        self.assertNotIn(r.value, str(self.w.sys.history.query()))

    def test_FR_COM_01_registering_records_who_and_what_changed(self):
        self.cfg.register_adc(self.admin, "ADC-05")
        e = self.w.sys.history.query(kind="adc_registered")[-1]
        self.assertEqual((e.user_id, e.data["old"], e.data["new"]), ("admin1", "unregistered", "registered"))

    def test_deregister_unknown_adc_is_refused(self):
        self.assertFalse(self.cfg.deregister_adc(self.admin, "ADC-77").ok)

    def test_deregistration_fails_pending_threshold_changes(self):
        a = self.w.add_adc(); self.w.online(a)
        r = self.cfg.request_threshold_change(threshold_auth(self.w), "ADC-01", 3, 8)
        self.cfg.deregister_adc(self.admin, "ADC-01")
        self.assertEqual(r.value.status, "Failed")

    def test_ids_can_be_registered_again_after_deregistration(self):
        a = self.w.add_adc(); self.w.online(a)
        self.cfg.deregister_adc(self.admin, "ADC-01")
        self.assertTrue(self.cfg.register_adc(self.admin, "ADC-01", "new-key").ok)
        self.assertIs(self.w.sys.health.state_of("ADC-01").name, "OFFLINE")


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()
        self.w.online(self.adc)
        self.cfg = self.w.sys.config

    def req(self, slot=3, value=8, adc="ADC-01", uid="pharm1"):
        return self.cfg.request_threshold_change(threshold_auth(self.w, uid), adc, slot, value)

    def test_T1_refused_in_maintenance(self):
        self.w.sys.mode.switch_to_maintenance(self.w.auth("admin1", Fn.ADMIN))
        self.assertFalse(self.req().ok)

    def test_T2_only_pharmacist_authorisation_works(self):
        self.assertIsNone(self.w.sys.access.authorise(self.w.tokens["nurse1"], Fn.CHANGE_THRESHOLD))
        self.assertIsNone(self.w.sys.access.authorise(self.w.tokens["tech1"], Fn.CHANGE_THRESHOLD))
        self.assertFalse(self.cfg.request_threshold_change(
            Authorisation("nurse1", Role.NURSE, Fn.VIEW_DASHBOARD), "ADC-01", 3, 8).ok)

    def test_T3_unknown_adc_is_refused(self):
        self.assertFalse(self.req(adc="ADC-77").ok)

    def test_T4_adc_that_is_not_online_is_refused_with_its_state(self):
        self.w.advance(31)
        r = self.req()
        self.assertFalse(r.ok)
        self.assertIn("Offline", r.reason)

    def test_T5_slot_that_the_adc_never_reported_is_refused(self):
        self.assertFalse(self.req(slot=9).ok)

    def test_T6_value_must_be_a_whole_number_from_zero_to_9999(self):
        for bad in (-1, 10000, 3.5, "7", None, True):
            self.assertFalse(self.req(value=bad).ok, repr(bad))
        self.assertTrue(self.req(value=0).ok)
        self.assertTrue(self.req(value=9999).ok)

    def test_T7_asking_for_the_confirmed_value_is_refused(self):
        self.assertFalse(self.req(value=5).ok)

    def test_T8_creates_an_unconfirmed_change_and_keeps_the_old_threshold(self):
        r = self.req(value=8)
        self.assertTrue(r.ok)
        self.assertEqual(r.value.status, "Unconfirmed")
        self.assertEqual(self.w.sys.inventory.confirmed_threshold("ADC-01", 3), 5)

    def test_FR_COM_05_configuration_is_sent_only_to_the_target_adc(self):
        b = self.w.add_adc("ADC-02"); self.w.online(b)
        self.req()
        self.adc.status(); b.status()
        self.assertEqual([m["threshold"] for m in self.adc.got("configuration")], [8])
        self.assertEqual(b.got("configuration"), [])

    def test_NFR_AUD_01_change_records_user_old_new_and_time(self):
        self.req(value=8)
        e = self.w.sys.history.query(kind="threshold_change")[-1]
        self.assertEqual((e.user_id, e.data["old"], e.data["new"]), ("pharm1", 5, 8))
        self.assertEqual(e.ts, self.w.clock.now())

    def test_C2_acknowledgement_confirms_and_updates_low_flags(self):
        self.adc.slots[3][1] = 7
        self.adc.inventory()
        self.assertFalse(self.w.sys.inventory.is_low("ADC-01", 3))
        c = self.req(value=8).value
        self.adc.ack(c.config_id, 3, 8)
        self.assertEqual(self.cfg.latest_change("ADC-01", 3).status, "Confirmed")
        self.assertEqual(self.w.sys.inventory.confirmed_threshold("ADC-01", 3), 8)
        self.assertTrue(self.w.sys.inventory.is_low("ADC-01", 3))

    def test_C1_acknowledgement_that_matches_nothing_is_ignored_and_recorded(self):
        c = self.req(value=8).value
        self.adc.ack(999, 3, 8)
        self.adc.ack(c.config_id, 4, 8)
        self.adc.ack(c.config_id, 3, 9)
        self.assertEqual(self.cfg.latest_change("ADC-01", 3).status, "Unconfirmed")
        self.assertGreaterEqual(len(self.w.sys.history.query(kind="ignored_event")), 3)

    def test_C1_acknowledgement_from_a_different_adc_is_ignored(self):
        b = self.w.add_adc("ADC-02"); self.w.online(b)
        c = self.req(value=8).value
        b.ack(c.config_id, 3, 8)
        self.assertEqual(self.cfg.latest_change("ADC-01", 3).status, "Unconfirmed")

    def test_C4_second_acknowledgement_is_ignored(self):
        c = self.req(value=8).value
        self.adc.ack(c.config_id, 3, 8)
        self.adc.ack(c.config_id, 3, 8)
        self.assertEqual(self.cfg.latest_change("ADC-01", 3).status, "Confirmed")

    def test_FR_COM_07_no_acknowledgement_in_sixty_seconds_fails_and_raises_a_medium_alarm(self):
        c = self.req(value=8).value
        for _ in range(7):
            self.w.advance(9); self.adc.status()
        self.w.advance(3)
        self.assertEqual(self.cfg.latest_change("ADC-01", 3).status, "Failed")
        al = [a for a in self.w.sys.alarms.active_alarms() if a.alarm_type == "threshold"]
        self.assertEqual(len(al), 1)
        self.assertIs(al[0].severity, Severity.MEDIUM)
        self.assertEqual(self.w.sys.inventory.confirmed_threshold("ADC-01", 3), 5)

    def test_C3_late_acknowledgement_of_a_failed_change_confirms_it_and_clears_the_alarm(self):
        c = self.req(value=8).value
        for _ in range(8):
            self.w.advance(9); self.adc.status()
        self.assertEqual(self.cfg.latest_change("ADC-01", 3).status, "Failed")
        self.adc.ack(c.config_id, 3, 8)
        self.assertEqual(self.cfg.latest_change("ADC-01", 3).status, "Confirmed")
        self.assertEqual([a for a in self.w.sys.alarms.active_alarms() if a.alarm_type == "threshold"], [])

    def test_a_newer_change_supersedes_the_unconfirmed_one(self):
        c1 = self.req(value=8).value
        c2 = self.req(value=9).value
        self.assertEqual([c.status for c in self.cfg.changes("ADC-01")], ["Superseded", "Unconfirmed"])
        self.adc.ack(c1.config_id, 3, 8)                                  # ignored: Superseded
        self.assertEqual(self.w.sys.inventory.confirmed_threshold("ADC-01", 3), 5)
        self.adc.ack(c2.config_id, 3, 9)
        self.assertEqual(self.w.sys.inventory.confirmed_threshold("ADC-01", 3), 9)

    def test_INV_CFG_1_two_unconfirmed_changes_for_one_slot_is_a_fail_safe(self):
        from monitoring.config_manager import ThresholdChange
        from monitoring.domain import FailSafe
        c = self.req(value=8).value
        self.cfg._changes[99] = ThresholdChange(99, "ADC-01", 3, 5, 7, "Unconfirmed", "x", 0.0)
        with self.assertRaises(FailSafe):
            self.cfg._check_invariant()


if __name__ == "__main__":
    unittest.main()
