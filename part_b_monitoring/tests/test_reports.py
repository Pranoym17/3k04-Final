"""Report Generator: FR-RPT-01..03, FR-HIS-04."""
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.domain import Authorisation, Role


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()
        self.w.online(self.adc)
        self.rg = self.w.sys.reports
        self.t0 = self.w.clock.now()

    def pharm(self, fn):
        return self.w.auth("pharm1", fn)

    def tech(self, fn):
        return self.w.auth("tech1", fn)

    def test_FR_RPT_01_inventory_report_lists_every_low_slot_of_every_adc(self):
        b = self.w.add_adc("ADC-02", slots=((1, "Heparin", 1),)); self.w.online(b)
        self.adc.slots[4][1] = 3
        self.adc.inventory()
        rep = self.rg.inventory_report(self.pharm(Fn.RUN_INVENTORY_REPORT)).value
        self.assertEqual([(r[0], r[1]) for r in rep.rows], [("ADC-01", 4), ("ADC-02", 1)])
        self.assertEqual(rep.columns[:3], ["ADC", "Slot", "Medication"])

    def test_FR_RPT_01_rows_of_an_adc_that_is_not_online_are_labelled_stale(self):
        self.adc.slots[4][1] = 3
        self.adc.inventory()
        self.w.advance(31)
        rep = self.rg.inventory_report(self.pharm(Fn.RUN_INVENTORY_REPORT)).value
        self.assertEqual({r[5] for r in rep.rows}, {"Stale"})

    def test_FR_RPT_02_alarm_report_has_type_severity_and_time_to_acknowledgement(self):
        self.adc.alarm("A-1", "door open", "high")
        self.w.clock.advance(42)
        self.w.sys.alarms.acknowledge(self.w.auth("tech1", Fn.ACK_ALARM), "ADC-01/A-1")
        self.adc.alarm("A-2", "temperature", "low")
        rep = self.rg.alarm_report(self.tech(Fn.RUN_ALARM_AVAIL_REPORTS), self.t0 - 1, self.t0 + 100).value
        rows = {r[1]: r for r in rep.rows}
        self.assertEqual(rows["A-1"][7], "42 s")
        self.assertEqual(rows["A-2"][7], "not acknowledged")
        self.assertEqual((rows["A-1"][2], rows["A-1"][3]), ("door open", "High"))

    def test_FR_RPT_02_range_and_adc_filter(self):
        b = self.w.add_adc("ADC-02"); self.w.online(b)
        self.adc.alarm("A-1"); self.w.clock.advance(100); b.alarm("B-1")
        a = self.tech(Fn.RUN_ALARM_AVAIL_REPORTS)
        later = [r[1] for r in self.rg.alarm_report(a, self.t0 + 50, self.t0 + 200).value.rows]
        self.assertIn("B-1", later)
        self.assertNotIn("A-1", later)
        only_one = [r[1] for r in self.rg.alarm_report(a, self.t0 - 1, self.t0 + 200, "ADC-01").value.rows]
        self.assertIn("A-1", only_one)
        self.assertNotIn("B-1", only_one)

    def test_FR_RPT_03_availability_counts_offline_periods_and_time(self):
        w = World(); adc = w.add_adc()                       # registered at t0, Offline (A3)
        w.advance(120)
        w.online(adc)                                          # first period: 120 s
        for _ in range(3):
            w.advance(10); adc.status()
        w.advance(70)                                          # silence: Offline 30 s after the last message
        adc.status(); adc.inventory()                          # second period ends
        rep = w.sys.reports.availability_report(w.auth("tech1", Fn.RUN_ALARM_AVAIL_REPORTS),
                                                0, w.clock.now()).value
        self.assertEqual(len(rep.rows), 1)
        adc_id, n, total = rep.rows[0]
        self.assertEqual((adc_id, n), ("ADC-01", 2))
        minutes = float(total.split()[0])
        self.assertTrue(2.6 < minutes < 2.8, total)             # 120 s after registration + 40 s of silence = 2.67 min

    def test_FR_RPT_03_reconnecting_time_counts_as_offline(self):
        w = World(); adc = w.add_adc()
        adc.status()                                           # Reconnecting, still part of the Offline period
        w.advance(25)
        rep = w.sys.reports.availability_report(w.auth("tech1", Fn.RUN_ALARM_AVAIL_REPORTS), 0, w.clock.now()).value
        self.assertEqual(rep.rows[0][1], 1)
        self.assertGreaterEqual(float(rep.rows[0][2].split()[0]), 0.4)

    def test_reports_refuse_the_wrong_function_and_a_backwards_range(self):
        wrong = Authorisation("nurse1", Role.NURSE, Fn.VIEW_DASHBOARD)
        self.assertFalse(self.rg.inventory_report(wrong).ok)
        self.assertFalse(self.rg.alarm_report(wrong, 0, 1).ok)
        self.assertFalse(self.rg.availability_report(wrong, 0, 1).ok)
        self.assertFalse(self.rg.alarm_report(self.tech(Fn.RUN_ALARM_AVAIL_REPORTS), 10, 1).ok)

    def test_FR_HIS_04_dispensing_list_shows_events_and_marks_late_replays(self):
        self.adc.send("dispensing", slot=1, medication="Morphine", quantity=2, adc_user_id="RN-7")
        self.adc.send("dispensing", ts=self.t0 - 90, slot=1, medication="Morphine", quantity=1, adc_user_id="RN-8")
        rep = self.rg.dispensing_list(self.tech(Fn.VIEW_HISTORY), "ADC-01", self.t0 - 200, self.t0 + 10).value
        by_user = {r[3]: r for r in rep.rows}
        self.assertEqual(by_user["RN-7"][:3], [1, "Morphine", 2])
        self.assertEqual(by_user["RN-7"][5], "")
        self.assertEqual(by_user["RN-8"][5], "Late")
        self.assertNotIn("patient", " ".join(rep.columns).lower())

    def test_FR_HIS_04_a_rejected_dispensing_message_is_not_listed(self):
        self.adc.send("dispensing", slot=1, medication="M", quantity=1, adc_user_id="RN-7", patient_id="P1")
        rep = self.rg.dispensing_list(self.tech(Fn.VIEW_HISTORY), "ADC-01", self.t0 - 1, self.t0 + 10).value
        self.assertEqual(rep.rows, [])

    def test_csv_has_a_header_row_and_one_line_per_row(self):
        self.adc.slots[4][1] = 3
        self.adc.inventory()
        rep = self.rg.inventory_report(self.pharm(Fn.RUN_INVENTORY_REPORT)).value
        lines = self.rg.to_csv(rep).strip().splitlines()
        self.assertEqual(len(lines), 1 + len(rep.rows))
        self.assertTrue(lines[0].startswith("ADC,Slot,Medication"))


if __name__ == "__main__":
    unittest.main()
