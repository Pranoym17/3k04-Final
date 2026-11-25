"""Cabinet Health Monitor: every cell of CONN_TABLE plus the named behaviours (FR-CHM-*)."""
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.domain import (AdcMode, AlarmState, Category, ConnState, FailSafe, Origin, Severity)
from monitoring.health_monitor import CONN_TABLE, ConnEvent, _Rec
from monitoring.transition_table import Illegal, Ignore, Move, Refuse, Stay, TransitionTable

S, E = ConnState, ConnEvent


def comm_alarms(w, adc_id="ADC-01", active=True):
    return [a for a in w.sys.alarms.active_alarms()
            if a.adc_id == adc_id and a.alarm_type == "communication"]


def put_in_state(w, adc, state):
    if state is S.OFFLINE:
        pass                                       # a new registration is Offline (A3)
    elif state is S.RECONNECTING:
        adc.status()
    elif state is S.ONLINE:
        adc.status(); adc.inventory()
    assert w.sys.health.state_of(adc.id) is state, (state, w.sys.health.state_of(adc.id))


def apply_event(w, adc, ev):
    if ev is E.REGISTER:
        w.sys.health.on_registered(adc.id)
    elif ev is E.DEREGISTER:
        w.sys.config.deregister_adc(w.auth("admin1", Fn.ADMIN), adc.id)
    elif ev is E.STATUS:
        adc.status()
    elif ev is E.INVENTORY:
        adc.inventory()
    elif ev is E.OTHER:
        adc.alarm("A-1", severity="low")
    elif ev is E.LATE:
        adc.send("status", ts=w.clock.now() - 120, mode="Normal")
    elif ev is E.INVALID:
        adc.status(bad_auth=True)
    elif ev is E.TIMEOUT:
        w.advance(w.sys.policy.offline_timeout_s + 1)
    elif ev is E.RESTART:
        w.sys.health.start()


class ConnectionTableTests(unittest.TestCase):
    def test_table_has_a_cell_for_every_state_and_event(self):
        self.assertEqual(len(list(CONN_TABLE.cells())), len(S) * len(E))

    def test_a_blank_cell_is_rejected_when_the_table_is_built(self):
        with self.assertRaises(ValueError):
            TransitionTable("t", list(S), list(E), {s: {E.STATUS: Stay()} for s in S})

    def test_every_action_named_in_the_table_is_implemented(self):
        w = World()
        w.add_adc()
        for s, e, cell in CONN_TABLE.cells():
            action = getattr(cell, "action", "")
            if action:
                w.sys.health._run(action, "ADC-01", _Rec(), w.clock.now(), None)    # no FailSafe

    def test_unknown_action_is_a_fail_safe(self):
        w = World()
        with self.assertRaises(FailSafe):
            w.sys.health._run("no_such_action", "ADC-01", _Rec(), 0.0, None)


def _make_cell_test(state, event, cell):
    def test(self):
        w = World()
        adc = w.add_adc()
        if state is S.UNREGISTERED:
            w2 = w
            if event is E.REGISTER:
                w.sys.config.deregister_adc(w.auth("admin1", Fn.ADMIN), adc.id)
                w.sys.health.on_registered(adc.id)
                self.assertIs(w.sys.health.state_of(adc.id), S.OFFLINE)
            elif isinstance(cell, Illegal):
                from monitoring.domain import AdcMessage, MsgKind, StatusBody
                m = AdcMessage(MsgKind.STATUS, "ADC-77", 1, 0.0, 0.0, event is E.LATE, StatusBody(AdcMode.NORMAL))
                with self.assertRaises(FailSafe):
                    w.sys.health.on_message(m)
            else:                                   # Ignore / Stay: nothing happens, nothing breaks
                w.sys.health.on_deregistered("ADC-77")
                w.sys.health.on_invalid_message("ADC-77")
                w.sys.health.tick(0.0)
                self.assertIs(w.sys.health.state_of("ADC-77"), S.UNREGISTERED)
            return
        put_in_state(w, adc, state)
        before = len(w.sys.history.query(adc_id=adc.id))
        apply_event(w, adc, event)
        after = w.sys.health.state_of(adc.id)
        if isinstance(cell, Move):
            self.assertIs(after, cell.next, f"{state.name}/{event.name}")
            ch = w.sys.history.query(adc_id=adc.id, kind="connection_change")
            self.assertEqual((ch[-1].data["from"], ch[-1].data["to"]), (state.value, cell.next.value))
        else:
            self.assertIs(after, state, f"{state.name}/{event.name}")
            if isinstance(cell, Refuse):
                self.assertTrue(w.sys.history.query(adc_id=adc.id, kind="refused_event"))
    return test


for _s, _e, _c in CONN_TABLE.cells():
    setattr(ConnectionTableTests, f"test_cell_{_s.name}_{_e.name}", _make_cell_test(_s, _e, _c))


class HealthBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()

    def state(self):
        return self.w.sys.health.state_of("ADC-01")

    def test_A3_new_registration_is_offline_with_a_communication_alarm(self):
        self.assertIs(self.state(), S.OFFLINE)
        al = comm_alarms(self.w)
        self.assertEqual(len(al), 1)
        self.assertIs(al[0].origin, Origin.MONITORING)
        self.assertIs(al[0].severity, Severity.HIGH)

    def test_FR_CHM_01_status_every_ten_seconds_keeps_the_adc_online(self):
        self.w.online(self.adc)
        for _ in range(12):
            self.w.advance(10)
            self.adc.status()
        self.assertIs(self.state(), S.ONLINE)
        self.assertEqual(comm_alarms(self.w), [])

    def test_FR_CHM_02_thirty_seconds_of_silence_makes_it_offline_with_a_high_alarm(self):
        self.w.online(self.adc)
        self.w.advance(29.5)
        self.assertIs(self.state(), S.ONLINE)
        self.w.advance(1)
        self.assertIs(self.state(), S.OFFLINE)
        self.assertEqual(len(comm_alarms(self.w)), 1)
        self.assertIs(comm_alarms(self.w)[0].severity, Severity.HIGH)

    def test_NFR_SAF_02_overdue_adc_is_never_reported_online_even_without_a_tick(self):
        self.w.online(self.adc)
        self.w.clock.advance(31)                       # no tick() call
        self.assertIs(self.state(), S.OFFLINE)
        self.assertTrue(self.w.sys.health.snapshot("ADC-01").stale)

    def test_FR_CHM_03_offline_and_reconnecting_data_is_stale_online_is_not(self):
        h = self.w.sys.health
        self.assertTrue(h.is_stale("ADC-01"))
        self.adc.status()
        self.assertTrue(h.is_stale("ADC-01"))
        self.adc.inventory()
        self.assertFalse(h.is_stale("ADC-01"))

    def test_FR_CHM_04_first_status_after_silence_only_starts_reconnecting(self):
        self.adc.status()
        self.assertIs(self.state(), S.RECONNECTING)
        self.adc.status()
        self.assertIs(self.state(), S.RECONNECTING)

    def test_FR_COM_11_monitoring_system_asks_for_a_fresh_inventory_on_reconnect(self):
        self.adc.status()
        self.assertEqual(len(self.adc.got("inventory_request")), 1)

    def test_FR_CHM_05_fresh_inventory_completes_recovery(self):
        self.adc.status()
        self.adc.inventory()
        self.assertIs(self.state(), S.ONLINE)

    def test_FR_CHM_05_no_inventory_within_thirty_seconds_returns_to_offline_alarm_stays(self):
        self.adc.status()
        self.w.advance(31)
        self.assertIs(self.state(), S.OFFLINE)
        self.assertEqual(len(comm_alarms(self.w)), 1)

    def test_FR_CHM_06_communication_alarm_clears_by_itself_on_recovery(self):
        self.w.online(self.adc)
        self.assertEqual(comm_alarms(self.w), [])
        cleared = [e for e in self.w.sys.history.query(adc_id="ADC-01", kind="alarm_change")
                   if e.data["to"] == AlarmState.CLEARED.value]
        self.assertEqual(len(cleared), 1)

    def test_FR_CHM_07_fault_status_raises_one_high_fault_alarm_per_condition(self):
        self.w.online(self.adc)
        self.adc.status("Fault")
        self.adc.status("Fault")
        faults = [a for a in self.w.sys.alarms.active_alarms() if a.alarm_type == "fault"]
        self.assertEqual(len(faults), 1)
        self.assertIs(faults[0].severity, Severity.HIGH)

    def test_FR_CHM_08_return_to_normal_clears_the_fault_alarm(self):
        self.w.online(self.adc)
        self.adc.status("Fault")
        self.adc.status("Normal")
        self.assertEqual([a for a in self.w.sys.alarms.active_alarms() if a.alarm_type == "fault"], [])
        self.assertIs(self.w.sys.health.snapshot("ADC-01").adc_mode, AdcMode.NORMAL)

    def test_D3_a_late_message_never_restarts_the_timer(self):
        self.w.online(self.adc)
        self.w.advance(20)
        self.adc.send("status", ts=self.w.clock.now() - 100, mode="Normal")     # Late
        self.w.advance(10.5)
        self.assertIs(self.state(), S.OFFLINE)

    def test_D3_a_late_message_does_not_bring_an_offline_adc_back(self):
        self.adc.send("status", ts=self.w.clock.now() - 100, mode="Normal")
        self.assertIs(self.state(), S.OFFLINE)

    def test_invalid_message_from_a_registered_adc_does_not_restart_the_timer(self):
        self.w.online(self.adc)
        self.w.advance(20)
        self.adc.status(bad_auth=True)
        self.w.advance(10.5)
        self.assertIs(self.state(), S.OFFLINE)

    def test_FR_CHM_01_any_valid_message_counts_as_proof_of_life_when_online(self):
        self.w.online(self.adc)
        self.w.advance(25)
        self.adc.alarm("A-9", severity="low")
        self.w.advance(25)
        self.assertIs(self.state(), S.ONLINE)

    def test_FR_COM_12_deregistering_removes_the_adc_from_health(self):
        self.w.online(self.adc)
        self.w.sys.config.deregister_adc(self.w.auth("admin1", Fn.ADMIN), "ADC-01")
        self.assertIs(self.state(), S.UNREGISTERED)

    def test_one_adc_going_offline_does_not_affect_another(self):
        b = self.w.add_adc("ADC-02")
        self.w.online(self.adc); self.w.online(b)
        self.w.advance(20)
        b.status()
        self.w.advance(11)
        self.assertIs(self.state(), S.OFFLINE)
        self.assertIs(self.w.sys.health.state_of("ADC-02"), S.ONLINE)


if __name__ == "__main__":
    unittest.main()
