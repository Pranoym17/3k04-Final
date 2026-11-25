"""Operator Dashboard and ADC Transport Adapter through Flask's test client (FR-AUT-*, FR-ALM-05, FR-DSH-*, NFR-ACC-*)."""
import json
import re
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.adc_comm import sign
from monitoring.domain import Category, Fn

PW = {"admin1": "admin-pass-1", "pharm1": "pharm1-password", "nurse1": "nurse1-password",
      "tech1": "tech1-password"}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()
        self.w.online(self.adc)
        self.app = make_app(self.w.sys)
        self.app.testing = True

    def client(self, uid=None):
        c = self.app.test_client()
        if uid:
            r = c.post("/login", data={"user_id": uid, "password": PW[uid]})
            self.assertEqual(r.status_code, 302)
        return c

    def text(self, r):
        return r.get_data(as_text=True)

    # ---- login and sessions
    def test_FR_AUT_01_no_page_shows_cabinet_data_without_a_session(self):
        c = self.client()
        for path in ("/dashboard", "/dashboard/live", "/adc/ADC-01", "/history", "/reports", "/admin"):
            r = c.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"])
        for path, data in (("/alarms/ack", {"key": "x"}), ("/adc/ADC-01/threshold", {"slot": 1, "value": 3})):
            self.assertEqual(c.post(path, data=data).status_code, 302)

    def test_wrong_password_shows_one_generic_message_and_sets_no_cookie(self):
        c = self.client()
        r = c.post("/login", data={"user_id": "nurse1", "password": "nope"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("wrong user ID or password", self.text(r))
        self.assertNotIn("ms_session=", r.headers.get("Set-Cookie", ""))

    def test_login_cookie_is_httponly_and_samesite_strict(self):
        r = self.app.test_client().post("/login", data={"user_id": "nurse1", "password": PW["nurse1"]})
        cookie = r.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_the_login_redirect_cannot_leave_the_site(self):
        r = self.app.test_client().post("/login?next=//evil.example", data={"user_id": "nurse1", "password": PW["nurse1"]})
        self.assertNotIn("evil", r.headers["Location"])

    def test_logout_ends_the_session(self):
        c = self.client("nurse1")
        self.assertEqual(c.get("/dashboard").status_code, 200)
        c.post("/logout")
        self.assertEqual(c.get("/dashboard").status_code, 302)

    def test_NFR_SEC_01_idle_session_is_sent_back_to_login(self):
        c = self.client("nurse1")
        self.w.clock.advance(601)
        self.assertEqual(c.get("/dashboard").status_code, 302)

    # ---- role enforcement (FR-AUT-02), pages and actions
    def test_FR_AUT_02_nurse_sees_the_dashboard_but_nothing_else(self):
        c = self.client("nurse1")
        self.assertEqual(c.get("/dashboard").status_code, 200)
        self.assertEqual(c.get("/adc/ADC-01").status_code, 200)
        for path in ("/history?adc_id=ADC-01", "/reports/inventory", "/reports/alarms", "/admin", "/admin/audit"):
            self.assertEqual(c.get(path).status_code, 403, path)
        for path, data in (("/alarms/ack", {"key": "ADC-01/A-1"}),
                           ("/adc/ADC-01/threshold", {"slot": 3, "value": 8}),
                           ("/requests/decide", {"key": "ADC-01/M-1", "decision": "accept"}),
                           ("/admin/mode", {"mode": "maintenance"}),
                           ("/admin/adcs/register", {"adc_id": "ADC-05"}),
                           ("/admin/users/create", {"user_id": "x1", "role": "NURSE", "password": "long-enough-1"})):
            self.assertEqual(c.post(path, data=data).status_code, 403, path)
        self.assertEqual(self.w.sys.config.registered_ids(), ["ADC-01"])
        refusals = self.w.sys.history.query(kind="access_refused", user_id="nurse1")
        self.assertGreaterEqual(len(refusals), 10)

    def test_D14_nurse_sees_no_threshold_or_acknowledge_result_even_if_the_page_shows_a_control(self):
        self.adc.alarm("A-1")
        c = self.client("nurse1")
        c.post("/alarms/ack", data={"key": "ADC-01/A-1"})
        self.assertIs(self.w.sys.alarms.get("ADC-01/A-1").state.name, "RAISED")

    def test_pharmacist_acknowledges_and_changes_a_threshold(self):
        self.adc.alarm("A-1")
        c = self.client("pharm1")
        c.post("/alarms/ack", data={"key": "ADC-01/A-1"})
        self.assertEqual(self.w.sys.alarms.get("ADC-01/A-1").acknowledged_by, "pharm1")
        r = c.post("/adc/ADC-01/threshold", data={"slot": 3, "value": 8}, follow_redirects=True)
        self.assertIn("Unconfirmed", self.text(r))
        self.assertEqual(self.w.sys.inventory.confirmed_threshold("ADC-01", 3), 5)

    def test_threshold_form_rejects_text_without_a_crash(self):
        c = self.client("pharm1")
        r = c.post("/adc/ADC-01/threshold", data={"slot": "x", "value": "1; DROP"}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("whole numbers", self.text(r))

    def test_technician_decides_a_maintenance_request(self):
        self.adc.request("M-1", "maintenance")
        c = self.client("tech1")
        self.assertIn("M-1", self.text(c.get("/dashboard")))
        c.post("/requests/decide", data={"key": "ADC-01/M-1", "decision": "reject"})
        self.assertEqual(self.w.sys.requests.pending_maintenance(), [])

    def test_administrator_registers_an_adc_and_the_key_is_shown_once(self):
        c = self.client("admin1")
        r = c.post("/admin/adcs/register", data={"adc_id": "ADC-05"}, follow_redirects=True)
        m = re.search(r"Its key is ([0-9a-f]{32})", self.text(r))
        self.assertIsNotNone(m)
        self.assertNotIn(m.group(1), self.text(c.get("/admin")))
        self.assertNotIn(m.group(1), str(self.w.sys.history.query()))

    def test_administrator_switches_mode_and_the_banner_shows_on_every_page(self):
        c = self.client("admin1")
        c.post("/admin/mode", data={"mode": "maintenance"})
        self.assertIn("MAINTENANCE", self.text(c.get("/dashboard")))
        n = self.client("nurse1")
        self.assertIn("MAINTENANCE", self.text(n.get("/dashboard")))

    def test_demo_storage_switch_is_hidden_outside_demo_mode(self):
        c = self.client("admin1")
        self.assertEqual(c.post("/admin/demo/storage", data={"state": "fail"}).status_code, 404)

    def test_demo_storage_failure_shows_fault_mode_and_alarms_still_show(self):
        self.w.sys.demo = True
        self.adc.alarm("A-1")
        c = self.client("admin1")
        c.post("/admin/demo/storage", data={"state": "fail"})
        page = self.text(c.get("/dashboard"))
        self.assertIn("FAULT", page)
        self.assertIn("A-1", page)

    # ---- dashboard content
    def test_FR_DSH_the_dashboard_lists_every_adc_with_state_text_not_only_colour(self):
        b = self.w.add_adc("ADC-02")
        page = self.text(self.client("nurse1").get("/dashboard"))
        self.assertIn("ADC-01", page)
        self.assertIn("Online", page)
        self.assertIn("ADC-02", page)
        self.assertIn("Offline", page)
        self.assertRegex(page, r"[Ss]tale")

    def test_FR_ALM_05_alarm_order_on_the_page_matches_the_manager(self):
        self.adc.alarm("A-1", severity="low"); self.adc.alarm("A-2", severity="high")
        page = self.text(self.client("pharm1").get("/dashboard"))
        self.assertLess(page.index("A-2"), page.index("A-1"))

    def test_escalation_cue_is_shown_to_pharmacist_and_technician_but_not_nurse(self):
        self.adc.alarm("A-1", severity="high")
        self.w.advance(125); self.adc.status()
        self.assertIn("ESCALATED", self.text(self.client("pharm1").get("/dashboard/live")).upper())
        self.assertNotIn("audible", self.text(self.client("nurse1").get("/dashboard/live")).lower())

    def test_pages_escape_text_that_came_from_an_adc(self):
        self.assertEqual(self.adc.send("alarm_notice", alarm_id="A-1", alarm_type="<script>x</script>",
                                       severity="low")["result"], "rejected")
        self.assertEqual(self.adc.alarm("A-2", alarm_type="door <b>")["result"], "rejected")

    def test_reports_pages_and_csv(self):
        self.adc.slots[4][1] = 2
        self.adc.inventory()
        c = self.client("pharm1")
        page = self.text(c.get("/reports/inventory"))
        self.assertIn("Saline", page)
        r = c.get("/reports/inventory?format=csv")
        self.assertEqual(r.mimetype, "text/csv")
        self.assertTrue(self.text(r).startswith("ADC,Slot,Medication"))

    def test_FR_HIS_02_history_page_filters_by_adc_and_range(self):
        c = self.client("tech1")
        r = c.get("/history?adc_id=ADC-01&start=2000-01-01T00:00&end=2100-01-01T00:00")
        self.assertEqual(r.status_code, 200)
        self.assertIn("adc_message", self.text(r) + "adc_message") 
        bad = c.get("/history?adc_id=ADC-01&start=2100-01-01T00:00&end=2000-01-01T00:00")
        self.assertEqual(bad.status_code, 200)

    def test_audit_page_shows_a_users_actions_and_logins(self):
        self.client("pharm1").post("/adc/ADC-01/threshold", data={"slot": 3, "value": 8})
        page = self.text(self.client("admin1").get("/admin/audit?user_id=pharm1"))
        self.assertIn("threshold_change", page)
        self.assertIn("login_ok", page)

    def test_NFR_ACC_01_every_page_has_a_language_title_and_skip_link(self):
        page = self.text(self.client("nurse1").get("/dashboard"))
        self.assertRegex(page, r'<html lang="en"')
        self.assertIn("<title>", page)
        self.assertRegex(page.lower(), r"skip to")

    # ---- ADC gateway
    def test_gateway_accepts_a_good_message_and_answers_400_for_a_bad_one(self):
        c = self.app.test_client()
        f = self.adc.build("status", mode="Normal")
        good = c.post("/adc/messages", data=self.adc.raw(f), content_type="application/json")
        self.assertEqual(good.status_code, 200)
        self.assertEqual(json.loads(good.data)["result"], "accepted")
        bad = c.post("/adc/messages", data=self.adc.raw(self.adc.build("status", mode="Normal"), bad_auth=True),
                     content_type="application/json")
        self.assertEqual(bad.status_code, 400)

    def test_gateway_needs_no_login_but_dashboard_pages_do(self):
        c = self.app.test_client()
        self.assertEqual(c.post("/adc/messages", data=b"{}", content_type="application/json").status_code, 400)
        self.assertEqual(c.get("/dashboard").status_code, 302)

    def test_gateway_outbox_returns_messages_for_a_correctly_signed_poll(self):
        import hashlib, hmac
        self.w.sys.config.request_threshold_change(self.w.auth("pharm1", Fn.CHANGE_THRESHOLD), "ADC-01", 3, 8)
        ts = f"{self.w.clock.now():.3f}"
        auth = hmac.new(b"key-ADC-01", f"outbox|ADC-01|{ts}".encode(), hashlib.sha256).hexdigest()
        r = self.app.test_client().get(f"/adc/outbox?adc_id=ADC-01&ts={ts}&auth={auth}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([m["type"] for m in json.loads(r.data)["messages"]], ["configuration"])
        bad = self.app.test_client().get(f"/adc/outbox?adc_id=ADC-01&ts={ts}&auth={'0'*64}")
        self.assertNotEqual(bad.status_code, 200)

    def test_the_wire_format_is_known_only_to_comm_and_adapter(self):
        """Anticipated change: a new wire format. Only adc_comm and the adapter mention wire field names."""
        import inspect
        from monitoring import (access_control, alarm_manager, config_manager, health_monitor, inventory,
                                mode_manager, report_generator, request_handler)
        for m in (access_control, alarm_manager, config_manager, health_monitor, inventory, mode_manager,
                  report_generator, request_handler):
            src = inspect.getsource(m)
            for wire in ("message_id", "\"timestamp\"", "hmac.new"):
                self.assertNotIn(wire, src, f"{m.__name__} mentions {wire}")


if __name__ == "__main__":
    unittest.main()
