"""Inventory Supervisor: FR-INV-01, FR-INV-02, Low rule, ordering of reports."""
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.domain import FailSafe


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()                      # slots 1:20  3:12  4:30
        self.w.online(self.adc)
        self.inv = self.w.sys.inventory

    def test_FR_INV_01_report_replaces_the_whole_view(self):
        self.assertEqual([s.slot for s in self.inv.slots("ADC-01")], [1, 3, 4])
        self.adc.slots = {1: ["Morphine", 19]}
        self.adc.inventory()
        self.assertEqual([(s.slot, s.quantity) for s in self.inv.slots("ADC-01")], [(1, 19)])

    def test_FR_INV_01_quantity_equal_to_threshold_is_low(self):
        self.adc.slots[3][1] = 5
        self.adc.inventory()
        self.assertTrue(self.inv.is_low("ADC-01", 3))
        self.adc.slots[3][1] = 6
        self.adc.inventory()
        self.assertFalse(self.inv.is_low("ADC-01", 3))

    def test_FR_INV_01_zero_quantity_is_low(self):
        self.adc.slots[1][1] = 0
        self.adc.inventory()
        self.assertTrue(self.inv.is_low("ADC-01", 1))

    def test_default_threshold_is_five(self):
        self.assertEqual(self.inv.confirmed_threshold("ADC-01", 1), 5)

    def test_FR_INV_02_low_slots_are_counted_per_adc_and_listed_for_all(self):
        b = self.w.add_adc("ADC-02", slots=((1, "Heparin", 2), (2, "Saline", 40)))
        self.w.online(b)
        self.adc.slots[4][1] = 1
        self.adc.inventory()
        self.assertEqual(self.inv.low_count("ADC-01"), 1)
        self.assertEqual(self.inv.low_count("ADC-02"), 1)
        names = [(v.adc_id, v.slot) for v in self.inv.all_low_slots(["ADC-01", "ADC-02"])]
        self.assertEqual(names, [("ADC-01", 4), ("ADC-02", 1)])

    def test_each_slot_carries_adc_and_receive_time_of_its_report(self):
        self.w.clock.advance(7)
        self.adc.inventory()
        v = self.inv.slots("ADC-01")[0]
        self.assertEqual(v.receive_ts, self.w.clock.now())
        self.assertEqual(v.adc_ts, self.w.clock.now())

    def test_late_inventory_report_is_ignored_and_recorded(self):
        self.adc.slots[1][1] = 1
        self.adc.send("inventory", ts=self.w.clock.now() - 100,
                      slots=[{"slot": 1, "medication": "Morphine", "quantity": 1}])
        self.assertEqual(self.inv.slots("ADC-01")[0].quantity, 20)
        self.assertTrue(self.w.sys.history.query(kind="ignored_event", adc_id="ADC-01"))

    def test_report_older_than_the_applied_one_is_ignored(self):
        now = self.w.clock.now()
        self.w.clock.advance(10)
        self.adc.inventory()                              # applied, adc_ts = now+10
        self.adc.send("inventory", ts=now + 5, slots=[{"slot": 1, "medication": "Morphine", "quantity": 2}])
        self.assertEqual(self.inv.slots("ADC-01")[0].quantity, 20)

    def test_quantity_of_a_stale_adc_is_still_shown_with_its_old_times(self):
        self.w.advance(40)
        self.assertTrue(self.w.sys.health.is_stale("ADC-01"))
        self.assertEqual(len(self.inv.slots("ADC-01")), 3)

    def test_unknown_adc_has_no_slots(self):
        self.assertEqual(self.inv.slots("ADC-77"), [])
        self.assertFalse(self.inv.is_low("ADC-77", 1))

    def test_threshold_outside_range_is_a_fail_safe(self):
        with self.assertRaises(FailSafe):
            self.inv.apply_confirmed_threshold("ADC-01", 1, 10000)
        with self.assertRaises(FailSafe):
            self.inv.apply_confirmed_threshold("ADC-01", 1, -1)

    def test_deregistered_adc_view_is_dropped(self):
        self.w.sys.config.deregister_adc(self.w.auth("admin1", Fn.ADMIN), "ADC-01")
        self.assertEqual(self.inv.slots("ADC-01"), [])

    def test_only_this_module_knows_what_low_means(self):
        """Anticipated change: Low becomes a different rule. No other module compares quantity with a threshold."""
        import inspect
        from monitoring import alarm_manager, health_monitor, request_handler, report_generator
        for m in (alarm_manager, health_monitor, request_handler, report_generator):
            self.assertNotIn(".quantity <=", inspect.getsource(m))
            self.assertNotIn("quantity <", inspect.getsource(m))


if __name__ == "__main__":
    unittest.main()
