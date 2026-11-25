"""Alarm Manager: every cell of ALARM_TABLE plus FR-ALM-01..06."""
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.alarm_manager import ALARM_TABLE, AlarmEvent
from monitoring.domain import (AlarmState, Authorisation, Category, Fn, Origin, Role, Severity)
from monitoring.transition_table import Ignore, Move, Refuse, Stay

S, E = AlarmState, AlarmEvent
KEY = "ADC-01/A-17"


def state_of(w, key=KEY):
    return w.sys.alarms.get(key).state


def put_in_state(w, adc, state):
    adc.alarm("A-17", severity="high")
    if state is S.ESCALATED:
        w.advance(121)
        adc.status()                          # keep the ADC online while time passes
    elif state is S.ACKNOWLEDGED:
        assert w.sys.alarms.acknowledge(w.auth("pharm1", Fn.ACK_ALARM), KEY).ok
    elif state is S.CLEARED:
        adc.clear("A-17")
    assert state_of(w) is state


def apply_event(w, adc, ev):
    a = w.sys.alarms
    if ev is E.ACK:
        return a.acknowledge(w.auth("tech1", Fn.ACK_ALARM), KEY)
    if ev is E.ACK_REFUSED:
        return a.acknowledge(Authorisation("nurse1", Role.NURSE, Fn.VIEW_DASHBOARD), KEY)
    if ev is E.NOTICE_SAME:
        return adc.alarm("A-17", severity="high")
    if ev is E.ESCALATE_DUE:
        w.advance(121)
        return None
    if ev is E.CLEAR:
        return adc.clear("A-17")


class AlarmTableTests(unittest.TestCase):
    def test_table_is_complete(self):
        self.assertEqual(len(list(ALARM_TABLE.cells())), len(S) * len(E))


def _make(state, event, cell):
    def test(self):
        w = World()
        adc = w.add_adc()
        w.online(adc)
        put_in_state(w, adc, state)
        events_before = len(w.sys.history.query())
        res = apply_event(w, adc, event)
        if isinstance(cell, Move):
            self.assertIs(state_of(w), cell.next)
        else:
            self.assertIs(state_of(w), state, f"{state.name}/{event.name}")
            if isinstance(cell, Refuse):
                self.assertTrue(w.sys.history.query(kind="refused_event"))
                self.assertFalse(res.ok)
            if isinstance(cell, Ignore):
                self.assertTrue(w.sys.history.query(kind="ignored_event"))
        self.assertGreaterEqual(len(w.sys.history.query()), events_before)
    return test


for _s, _e, _c in ALARM_TABLE.cells():
    # an ESCALATED alarm that stays Escalated on a status refresh is covered by the cell itself
    setattr(AlarmTableTests, f"test_cell_{_s.name}_{_e.name}", _make(_s, _e, _c))


class AlarmBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()
        self.w.online(self.adc)

    def test_FR_ALM_01_notice_creates_an_alarm_with_adc_time_and_receive_time(self):
        ts = self.w.clock.now() - 3
        self.adc.send("alarm_notice", ts=ts, alarm_id="A-17", alarm_type="door open", severity="medium")
        v = self.w.sys.alarms.get(KEY)
        self.assertEqual((v.alarm_type, v.severity, v.adc_ts), ("door open", Severity.MEDIUM, ts))
        self.assertEqual(v.receive_ts, self.w.clock.now())

    def test_FR_ALM_02_only_pharmacist_and_technician_can_acknowledge(self):
        self.adc.alarm("A-17")
        self.assertIsNone(self.w.sys.access.authorise(self.w.tokens["nurse1"], Fn.ACK_ALARM))
        self.assertIsNotNone(self.w.sys.access.authorise(self.w.tokens["pharm1"], Fn.ACK_ALARM))
        self.assertIsNotNone(self.w.sys.access.authorise(self.w.tokens["tech1"], Fn.ACK_ALARM))
        self.assertIsNone(self.w.sys.access.authorise(self.w.tokens["admin1"], Fn.ACK_ALARM))

    def test_FR_ALM_02_acknowledgement_records_who_and_when(self):
        self.adc.alarm("A-17")
        self.w.clock.advance(5)
        self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), KEY)
        v = self.w.sys.alarms.get(KEY)
        self.assertEqual((v.acknowledged_by, v.acknowledged_at), ("pharm1", self.w.clock.now()))
        self.assertTrue(self.w.sys.history.query(kind="alarm_acknowledged"))

    def test_acknowledging_an_unknown_alarm_is_refused(self):
        self.assertFalse(self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), "ADC-01/none").ok)

    def test_FR_ALM_03_acknowledged_alarm_stays_listed_until_the_adc_clears_it(self):
        self.adc.alarm("A-17")
        self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), KEY)
        self.assertIn(KEY, [a.key for a in self.w.sys.alarms.active_alarms()])
        self.adc.clear("A-17")
        self.assertNotIn(KEY, [a.key for a in self.w.sys.alarms.active_alarms()])

    def test_FR_ALM_03_clear_for_an_unknown_alarm_is_ignored_and_recorded(self):
        self.adc.clear("A-404")
        self.assertTrue(self.w.sys.history.query(kind="ignored_event"))
        self.assertEqual(self.w.sys.alarms.active_count("ADC-01"), 0)

    def test_FR_ALM_04_high_alarm_escalates_after_two_minutes_and_not_before(self):
        self.adc.alarm("A-17", severity="high")
        self.w.advance(119)
        self.assertIs(state_of(self.w), S.RAISED)
        self.adc.status()
        self.w.advance(1.5)
        self.assertIs(state_of(self.w), S.ESCALATED)
        self.assertTrue(self.w.sys.alarms.has_escalated())

    def test_FR_ALM_04_medium_and_low_alarms_never_escalate(self):
        self.adc.alarm("A-1", severity="medium")
        self.adc.alarm("A-2", severity="low")
        for _ in range(3):
            self.w.advance(60)
            self.adc.status()
        self.assertIs(state_of(self.w, "ADC-01/A-1"), S.RAISED)
        self.assertIs(state_of(self.w, "ADC-01/A-2"), S.RAISED)

    def test_FR_ALM_04_acknowledged_alarm_does_not_escalate(self):
        self.adc.alarm("A-17")
        self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), KEY)
        self.w.advance(130)
        self.assertIs(state_of(self.w), S.ACKNOWLEDGED)

    def test_FR_ALM_05_list_shows_escalated_first_then_severity_then_newest(self):
        self.adc.alarm("A-1", severity="low")
        self.w.clock.advance(1)
        self.adc.alarm("A-2", severity="high")
        self.w.clock.advance(1)
        self.adc.alarm("A-3", severity="medium")
        self.w.clock.advance(1)
        self.adc.alarm("A-4", severity="medium")
        order = [a.alarm_id for a in self.w.sys.alarms.active_alarms()]
        self.assertEqual(order, ["A-2", "A-4", "A-3", "A-1"])
        self.w.advance(121); self.adc.status()
        self.adc.alarm("A-5", severity="high")
        self.assertEqual(self.w.sys.alarms.active_alarms()[0].alarm_id, "A-2")     # escalated first

    def test_FR_ALM_06_alarm_is_kept_in_history_after_it_clears(self):
        self.adc.alarm("A-17")
        self.adc.clear("A-17")
        kinds = [e.data["to"] for e in self.w.sys.history.query(kind="alarm_change", adc_id="ADC-01")
                 if e.data["alarm_id"] == "A-17"]
        self.assertEqual(kinds, ["Raised", "Cleared"])

    def test_same_alarm_id_on_two_adcs_are_two_alarms(self):
        b = self.w.add_adc("ADC-02"); self.w.online(b)
        self.adc.alarm("A-1"); b.alarm("A-1")
        self.assertEqual(len(self.w.sys.alarms.active_alarms()), 2)

    def test_D10_deregistering_an_adc_clears_all_its_alarms(self):
        self.adc.alarm("A-1"); self.adc.alarm("A-2", severity="low")
        self.w.sys.config.deregister_adc(self.w.auth("admin1", Fn.ADMIN), "ADC-01")
        self.assertEqual(self.w.sys.alarms.active_count("ADC-01"), 0)

    def test_monitoring_alarm_is_raised_once_per_condition(self):
        a = self.w.sys.alarms
        self.assertIsNotNone(a.raise_monitoring_alarm("ADC-01", "threshold", Severity.MEDIUM, "slot 3"))
        self.assertIsNone(a.raise_monitoring_alarm("ADC-01", "threshold", Severity.MEDIUM, "slot 3"))
        self.assertIsNotNone(a.raise_monitoring_alarm("ADC-01", "threshold", Severity.MEDIUM, "slot 4"))

    def test_the_adc_cannot_clear_a_monitoring_system_alarm(self):
        self.adc.status("Fault")
        mid = [a for a in self.w.sys.alarms.active_alarms() if a.alarm_type == "fault"][0].alarm_id
        self.adc.clear(mid)
        self.assertEqual(len([a for a in self.w.sys.alarms.active_alarms() if a.alarm_type == "fault"]), 1)

    def test_INV_ALM_1_only_high_alarms_can_be_escalated(self):
        self.adc.alarm("A-1", severity="medium")
        a = self.w.sys.alarms._alarms[ "ADC-01/A-1"]
        a.state = S.ESCALATED
        from monitoring.domain import FailSafe
        with self.assertRaises(FailSafe):
            self.w.sys.alarms._check_invariant()


if __name__ == "__main__":
    unittest.main()
