"""Demo account selection keeps normal sessions, roles and access checks."""
import unittest

from helpers import make_app
from monitoring.clock import ManualClock
from monitoring.domain import Fn
from monitoring.seed import ACCOUNTS, seed
from monitoring.system import MonitoringSystem


class DemoLoginTests(unittest.TestCase):
    def setUp(self):
        self.system = MonitoringSystem(clock=ManualClock(), demo=True)
        self.system.start()
        seed(self.system)
        self.app = make_app(self.system)
        self.app.testing = True

    def test_FR_AUT_01_demo_picker_creates_sessions_for_the_selected_accounts(self):
        for uid, role, password in ACCOUNTS:
            client = self.app.test_client()
            response = client.post("/login", data={"demo_account": uid})
            self.assertEqual(response.status_code, 302, uid)
            token = client.get_cookie("ms_session").value
            info = self.system.access.session(token)
            self.assertEqual((info.user_id, info.role), (uid, role))
        page = self.app.test_client().get("/login").get_data(as_text=True)
        self.assertIn("Choose an account", page)
        self.assertNotIn('type="password"', page)
        self.assertNotIn("12345", page)

    def test_FR_AUT_01_account_selection_is_unavailable_outside_demo_mode(self):
        self.system.demo = False
        client = self.app.test_client()
        response = client.post("/login", data={"demo_account": "admin1"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(client.get_cookie("ms_session"))
        self.assertIn('type="password"', response.get_data(as_text=True))

    def test_FR_AUT_02_picker_preserves_permissions_and_refuses_disabled_or_unknown_accounts(self):
        admin_token = self.system.access.login("admin1", "12345").value
        admin = self.system.access.authorise(admin_token, Fn.ADMIN)
        self.system.access.disable_user(admin, "tech2")
        for uid in ("tech2", "unknown"):
            client = self.app.test_client()
            self.assertEqual(client.post("/login", data={"demo_account": uid}).status_code, 200)
            self.assertIsNone(client.get_cookie("ms_session"))
        client = self.app.test_client()
        client.post("/login", data={"demo_account": "nurse1"})
        self.assertEqual(client.get("/dashboard").status_code, 200)
        self.assertEqual(client.get("/admin").status_code, 403)
        self.assertTrue(self.system.history.query(kind="login_ok", user_id="nurse1"))

    def test_NFR_SEC_01_expired_demo_session_can_select_an_account_again(self):
        client = self.app.test_client()
        client.post("/login", data={"demo_account": "pharm1"})
        self.system.clock.advance(self.system.policy.session_idle_s + 1)
        self.assertEqual(client.get("/dashboard/live").status_code, 302)
        response = client.post("/login?next=/dashboard/live", data={"demo_account": "nurse1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard")
        page = client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn('<div id="live"', page)
        self.assertIn('class="site-header"', page)

    def test_FR_AUT_01_entry_forms_always_submit_to_login_and_preserve_destination(self):
        for demo in (True, False):
            self.system.demo = demo
            page = self.app.test_client().get("/login?next=/dashboard").get_data(as_text=True)
            self.assertIn('action="/login?next=/dashboard"', page)
