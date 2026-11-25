"""Request Handler: refill rows Q1..Q4, maintenance requests (FR-REQ-01..05)."""
import unittest

from helpers import *                                    # noqa: F401,F403


def replies(adc):
    return adc.got("request_reply")


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()
        self.w.online(self.adc)
        self.adc.slots[3][1] = 2                           # slot 3 Low
        self.adc.inventory()

    def test_Q3_refill_for_a_low_slot_is_accepted(self):
        self.adc.request("R-1", "refill", slots=[3])
        r = replies(self.adc)[-1]
        self.assertEqual((r["request_id"], r["slot"], r["decision"]), ("R-1", 3, "accept"))

    def test_Q4_refill_for_a_slot_that_is_not_low_is_rejected(self):
        self.adc.request("R-2", "refill", slots=[1])
        r = replies(self.adc)[-1]
        self.assertEqual((r["decision"], r["reason"]), ("reject", "not Low"))

    def test_Q2_refill_for_an_unknown_slot_is_rejected(self):
        self.adc.request("R-3", "refill", slots=[9])
        self.assertEqual(replies(self.adc)[-1]["reason"], "unknown slot")

    def test_Q1_late_refill_is_rejected_as_too_old(self):
        self.adc.send("request", ts=self.w.clock.now() - 100, request_id="R-4", request_type="refill", slots=[3])
        self.assertEqual(replies(self.adc)[-1]["reason"], "request too old, send it again")

    def test_FR_REQ_01_one_reply_per_slot_in_a_multi_slot_request(self):
        self.adc.request("R-5", "refill", slots=[1, 3, 9])
        got = {r["slot"]: r["decision"] for r in replies(self.adc) if r["request_id"] == "R-5"}
        self.assertEqual(got, {1: "reject", 3: "accept", 9: "reject"})

    def test_the_decision_follows_the_confirmed_threshold(self):
        self.adc.request("R-6", "refill", slots=[1])
        self.assertEqual(replies(self.adc)[-1]["decision"], "reject")
        c = self.w.sys.config.request_threshold_change(self.w.auth("pharm1", Fn.CHANGE_THRESHOLD),
                                                       "ADC-01", 1, 25).value
        self.adc.ack(c.config_id, 1, 25)
        self.adc.request("R-7", "refill", slots=[1])
        self.assertEqual(replies(self.adc)[-1]["decision"], "accept")

    def test_a_repeated_request_id_is_not_answered_twice(self):
        self.adc.request("R-8", "refill", slots=[3])
        n = len([r for r in replies(self.adc) if r["request_id"] == "R-8"])
        self.adc.request("R-8", "refill", slots=[3])
        self.assertEqual(len([r for r in replies(self.adc) if r["request_id"] == "R-8"]), n)

    # ---- maintenance
    def test_FR_REQ_04_maintenance_request_waits_for_a_technician(self):
        self.adc.request("M-1", "maintenance")
        self.assertEqual([r.request_id for r in self.w.sys.requests.pending_maintenance()], ["M-1"])
        self.assertEqual([r for r in replies(self.adc) if r["request_id"] == "M-1"], [])

    def test_FR_REQ_04_technician_accepts_and_the_adc_gets_one_reply(self):
        self.adc.request("M-1", "maintenance")
        res = self.w.sys.requests.decide_maintenance(self.w.auth("tech1", Fn.DECIDE_MAINTENANCE),
                                                     "ADC-01/M-1", True)
        self.assertTrue(res.ok)
        self.adc.status()
        r = [x for x in replies(self.adc) if x["request_id"] == "M-1"]
        self.assertEqual([(x["decision"], x["reason"]) for x in r], [("accept", "decided by tech1")])

    def test_FR_REQ_04_first_technician_wins_and_the_second_is_refused(self):
        self.adc.request("M-1", "maintenance")
        k = "ADC-01/M-1"
        self.assertTrue(self.w.sys.requests.decide_maintenance(self.w.auth("tech1", Fn.DECIDE_MAINTENANCE), k, True).ok)
        self.assertFalse(self.w.sys.requests.decide_maintenance(self.w.auth("tech2", Fn.DECIDE_MAINTENANCE), k, False).ok)

    def test_only_a_technician_can_decide(self):
        for uid in ("pharm1", "nurse1", "admin1"):
            self.assertIsNone(self.w.sys.access.authorise(self.w.tokens[uid], Fn.DECIDE_MAINTENANCE))
        self.assertIsNotNone(self.w.sys.access.authorise(self.w.tokens["tech1"], Fn.DECIDE_MAINTENANCE))

    def test_FR_REQ_05_no_decision_within_five_minutes_rejects_the_request(self):
        self.adc.request("M-1", "maintenance")
        for _ in range(10):
            self.w.advance(29.9); self.adc.status()
        self.w.advance(2)
        self.assertEqual(self.w.sys.requests.pending_maintenance(), [])
        self.adc.status()
        r = [x for x in replies(self.adc) if x["request_id"] == "M-1"]
        self.assertEqual([(x["decision"], x["reason"]) for x in r], [("reject", "no decision")])

    def test_decision_after_the_timeout_is_refused(self):
        self.adc.request("M-1", "maintenance")
        for _ in range(11):
            self.w.advance(29.9); self.adc.status()
        res = self.w.sys.requests.decide_maintenance(self.w.auth("tech1", Fn.DECIDE_MAINTENANCE), "ADC-01/M-1", True)
        self.assertFalse(res.ok)

    def test_late_maintenance_request_is_rejected_at_once(self):
        self.adc.send("request", ts=self.w.clock.now() - 100, request_id="M-2", request_type="maintenance")
        self.assertEqual(self.w.sys.requests.pending_maintenance(), [])
        self.assertEqual(replies(self.adc)[-1]["reason"], "request too old, send it again")

    def test_decisions_are_refused_in_fault_mode(self):
        self.adc.request("M-1", "maintenance")
        self.w.sys.history.inject_failure(True)
        self.w.sys.history.append(__import__("monitoring.domain", fromlist=["Category"]).Category.SYSTEM, "p", {})
        res = self.w.sys.requests.decide_maintenance(self.w.auth("tech1", Fn.DECIDE_MAINTENANCE), "ADC-01/M-1", True)
        self.assertFalse(res.ok)


if __name__ == "__main__":
    unittest.main()
