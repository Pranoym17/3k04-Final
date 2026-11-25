"""System Mode Manager: every cell of MODE_TABLE, the ALLOWED function table, FR-MOD-*."""
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.domain import ChangeKind, Fn, SysMode
from monitoring.mode_manager import ALLOWED, MODE_TABLE, ModeEvent
from monitoring.transition_table import Illegal, Ignore, Move, Refuse

S, E = SysMode, ModeEvent


def reach(w, mode):
    mm = w.sys.mode
    if mode is S.NORMAL:
        pass
    elif mode is S.MAINTENANCE:
        assert mm.switch_to_maintenance(w.auth("admin1", Fn.ADMIN)).ok
    elif mode is S.FAULT:
        w.sys.history.inject_failure(True)
        w.sys.history.append(__import__("monitoring.domain", fromlist=["Category"]).Category.SYSTEM, "probe", {})
    elif mode is S.STARTING:
        mm.restart()
    assert mm.mode() is mode, (mode, mm.mode())


class ModeTableTests(unittest.TestCase):
    def test_table_is_complete(self):
        self.assertEqual(len(list(MODE_TABLE.cells())), len(S) * len(E))

    def test_ALLOWED_is_total_over_change_kinds_and_modes(self):
        for k in ChangeKind:
            for m in S:
                self.assertIn(m, ALLOWED[k])
        self.assertEqual(set(ALLOWED), set(ChangeKind))


def _make(state, event, cell):
    def test(self):
        w = World()
        reach(w, state)
        seen = []
        w.sys.mode.add_mode_listener(lambda a, b: seen.append((a, b)))
        res = w.sys.mode._dispatch(event, "test")
        w.sys.history.inject_failure(False)          # events written during Fault were buffered (H7)
        w.sys.history.test_write()
        if isinstance(cell, Move):
            self.assertIs(w.sys.mode.mode(), cell.next)
            self.assertEqual(seen, [(state, cell.next)])
            ch = w.sys.history.query(kind="mode_change")[-1]
            self.assertEqual((ch.data["from"], ch.data["to"]), (state.value, cell.next.value))
            self.assertEqual(bool(w.sys.mode.banner()), cell.next is S.FAULT or cell.next is S.MAINTENANCE)
        else:
            self.assertIs(w.sys.mode.mode(), state)
            self.assertEqual(seen, [])
            if isinstance(cell, Refuse):
                self.assertFalse(res.ok)
                self.assertTrue(w.sys.history.query(kind="refused_event"))
            if isinstance(cell, Ignore):
                self.assertTrue(res.ok)
                self.assertTrue(w.sys.history.query(kind="ignored_event"))
    return test


for _s, _e, _c in MODE_TABLE.cells():
    setattr(ModeTableTests, f"test_cell_{_s.name}_{_e.name}", _make(_s, _e, _c))


def _make_allowed(kind, mode):
    def test(self):
        w = World()
        reach(w, mode)
        self.assertEqual(w.sys.mode.allows(kind).ok, ALLOWED[kind][mode])
    return test


for _k in ChangeKind:
    for _m in S:
        setattr(ModeTableTests, f"test_allows_{_k.name}_in_{_m.name}", _make_allowed(_k, _m))


class ModeBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_FR_MOD_01_system_is_normal_after_a_successful_start_up_test_write(self):
        self.assertIs(self.w.sys.mode.mode(), S.NORMAL)

    def test_FR_MOD_05_only_the_administrator_can_switch_modes(self):
        self.assertIsNone(self.w.sys.access.authorise(self.w.tokens["tech1"], Fn.ADMIN))
        self.assertTrue(self.w.sys.mode.switch_to_maintenance(self.w.auth("admin1", Fn.ADMIN)).ok)

    def test_FR_MOD_03_maintenance_refuses_threshold_and_registration_changes(self):
        reach(self.w, S.MAINTENANCE)
        adc = self.w.add_adc.__self__  # noqa: F841
        r = self.w.sys.config.register_adc(self.w.auth("admin1", Fn.ADMIN), "ADC-09", "k")
        self.assertFalse(r.ok)
        self.assertIn("maintenance", r.reason.lower())

    def test_FR_MOD_03_acknowledgement_and_decisions_still_work_in_maintenance(self):
        a = self.w.add_adc(); self.w.online(a)
        a.alarm("A-17")
        reach(self.w, S.MAINTENANCE)
        self.assertTrue(self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), "ADC-01/A-17").ok)

    def test_FR_MOD_06_fault_refuses_acknowledgement_but_adc_alarms_still_show(self):
        a = self.w.add_adc(); self.w.online(a)
        a.alarm("A-17")
        reach(self.w, S.FAULT)
        self.assertFalse(self.w.sys.alarms.acknowledge(self.w.auth("pharm1", Fn.ACK_ALARM), "ADC-01/A-17").ok)
        a.alarm("A-18")                                                # still accepted and shown
        self.assertIn("ADC-01/A-18", [x.key for x in self.w.sys.alarms.active_alarms()])
        self.assertIn("FAULT", self.w.sys.mode.banner())

    def test_FR_MOD_04_fault_clears_when_the_test_write_succeeds_again(self):
        reach(self.w, S.FAULT)
        self.w.advance(5)
        self.assertIs(self.w.sys.mode.mode(), S.FAULT)
        self.w.sys.history.inject_failure(False)
        self.w.advance(11)
        self.assertIs(self.w.sys.mode.mode(), S.NORMAL)
        self.assertIsNone(self.w.sys.mode.banner())

    def test_fault_mode_keeps_testing_every_ten_seconds_while_storage_fails(self):
        reach(self.w, S.FAULT)
        self.w.advance(35)
        self.assertIs(self.w.sys.mode.mode(), S.FAULT)

    def test_switching_to_the_current_mode_is_ignored_and_recorded(self):
        r = self.w.sys.mode.switch_to_normal(self.w.auth("admin1", Fn.ADMIN))
        self.assertTrue(r.ok)
        self.assertTrue(self.w.sys.history.query(kind="ignored_event"))

    def test_mode_switch_records_who_changed_it(self):
        self.w.sys.mode.switch_to_maintenance(self.w.auth("admin1", Fn.ADMIN))
        e = self.w.sys.history.query(kind="mode_switch")[-1]
        self.assertEqual((e.user_id, e.data["new"]), ("admin1", "Maintenance"))

    def test_data_received_during_fault_is_kept_and_written_when_storage_returns(self):
        a = self.w.add_adc(); self.w.online(a)
        reach(self.w, S.FAULT)
        a.alarm("A-31")
        self.assertIn("ADC-01/A-31", [x.key for x in self.w.sys.alarms.active_alarms()])
        self.w.sys.history.inject_failure(False)
        self.w.advance(11)
        self.assertTrue(any(e.data.get("alarm_id") == "A-31"
                            for e in self.w.sys.history.query(kind="alarm_change")))


if __name__ == "__main__":
    unittest.main()
