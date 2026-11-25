"""Access Control: login rows L1..L5, authorisation rows A1..A3, role matrix, FR-AUT-*, NFR-SEC-01."""
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.access_control import PERMISSIONS
from monitoring.domain import Category, Fn, Role, SysMode


ROLE_MATRIX = {          # D1 section 8
    Fn.VIEW_DASHBOARD:          {"pharm1", "nurse1", "tech1", "admin1"},
    Fn.ACK_ALARM:               {"pharm1", "tech1"},
    Fn.CHANGE_THRESHOLD:        {"pharm1"},
    Fn.VIEW_HISTORY:            {"pharm1", "tech1", "admin1"},
    Fn.RUN_INVENTORY_REPORT:    {"pharm1"},
    Fn.RUN_ALARM_AVAIL_REPORTS: {"pharm1", "tech1"},
    Fn.DECIDE_MAINTENANCE:      {"tech1"},
    Fn.ADMIN:                   {"admin1"},
}


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.ac = self.w.sys.access

    def test_FR_AUT_02_role_matrix_is_exactly_the_d1_table(self):
        for fn, allowed in ROLE_MATRIX.items():
            for uid in ("pharm1", "nurse1", "tech1", "admin1"):
                got = self.ac.authorise(self.w.tokens[uid], fn) is not None
                self.assertEqual(got, uid in allowed, f"{uid} {fn.name}")

    def test_PERMISSIONS_covers_every_function(self):
        self.assertEqual(set(PERMISSIONS), set(Fn))

    def test_A1_no_session_no_authorisation(self):
        for tok in (None, "", "nonsense"):
            self.assertIsNone(self.ac.authorise(tok, Fn.VIEW_DASHBOARD))

    def test_A3_refusal_is_recorded_with_user_and_time(self):
        self.ac.authorise(self.w.tokens["nurse1"], Fn.CHANGE_THRESHOLD)
        e = self.w.sys.history.query(category=Category.ACCESS, kind="access_refused")[-1]
        self.assertEqual(e.user_id, "nurse1")
        self.assertEqual(e.ts, self.w.clock.now())

    def test_L5_right_password_gives_a_session_and_is_recorded(self):
        r = self.ac.login("nurse1", "nurse1-password")
        self.assertTrue(r.ok)
        self.assertTrue(self.w.sys.history.query(kind="login_ok", user_id="nurse1"))

    def test_L2_unknown_user_and_L3_wrong_password_give_the_same_answer(self):
        a = self.ac.login("nobody", "whatever-1")
        b = self.ac.login("nurse1", "wrong-password")
        self.assertEqual(a.reason, b.reason)
        self.assertFalse(a.ok or b.ok)

    def test_FR_AUT_03_five_wrong_passwords_lock_the_account_for_fifteen_minutes(self):
        for _ in range(5):
            self.assertFalse(self.ac.login("nurse1", "wrong-password").ok)
        r = self.ac.login("nurse1", "nurse1-password")                  # L1: correct password, still locked
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "account locked")
        self.w.clock.advance(899)
        self.assertFalse(self.ac.login("nurse1", "nurse1-password").ok)
        self.w.clock.advance(2)
        self.assertTrue(self.ac.login("nurse1", "nurse1-password").ok)

    def test_L4_a_good_login_resets_the_failure_count(self):
        for _ in range(4):
            self.ac.login("nurse1", "wrong-password")
        self.assertTrue(self.ac.login("nurse1", "nurse1-password").ok)
        for _ in range(4):
            self.ac.login("nurse1", "wrong-password")
        self.assertTrue(self.ac.login("nurse1", "nurse1-password").ok)

    def test_locking_one_account_does_not_lock_another(self):
        for _ in range(5):
            self.ac.login("nurse1", "wrong-password")
        self.assertTrue(self.ac.login("pharm1", "pharm1-password").ok)

    def test_NFR_SEC_01_session_ends_after_ten_idle_minutes(self):
        tok = self.ac.login("nurse1", "nurse1-password").value
        self.w.clock.advance(599)
        self.assertIsNotNone(self.ac.authorise(tok, Fn.VIEW_DASHBOARD))      # activity resets the timer
        self.w.clock.advance(599)
        self.assertIsNotNone(self.ac.authorise(tok, Fn.VIEW_DASHBOARD))
        self.w.clock.advance(601)
        self.assertIsNone(self.ac.authorise(tok, Fn.VIEW_DASHBOARD))
        self.assertTrue(self.w.sys.history.query(kind="session_expired"))

    def test_logout_ends_the_session(self):
        tok = self.ac.login("nurse1", "nurse1-password").value
        self.ac.logout(tok)
        self.assertIsNone(self.ac.authorise(tok, Fn.VIEW_DASHBOARD))

    def test_FR_AUT_04_create_user_needs_admin_one_role_and_a_long_enough_password(self):
        admin = self.w.auth("admin1", Fn.ADMIN)
        self.assertTrue(self.ac.create_user(admin, "nurse2", Role.NURSE, "long-enough-1").ok)
        self.assertFalse(self.ac.create_user(admin, "nurse2", Role.NURSE, "long-enough-1").ok)     # exists
        self.assertFalse(self.ac.create_user(admin, "nurse3", Role.NURSE, "short").ok)
        self.assertFalse(self.ac.create_user(admin, "bad id", Role.NURSE, "long-enough-1").ok)
        self.assertFalse(self.ac.create_user(admin, "nurse4", "nurse", "long-enough-1").ok)
        e = self.w.sys.history.query(kind="user_created")[-1]
        self.assertEqual((e.data["old"], e.data["new"]), (None, "Nurse"))

    def test_FR_AUT_05_disabled_account_cannot_log_in_and_its_sessions_end(self):
        tok = self.ac.login("nurse1", "nurse1-password").value
        self.assertTrue(self.ac.disable_user(self.w.auth("admin1", Fn.ADMIN), "nurse1").ok)
        self.assertIsNone(self.ac.authorise(tok, Fn.VIEW_DASHBOARD))
        self.assertFalse(self.ac.login("nurse1", "nurse1-password").ok)

    def test_account_changes_are_refused_in_fault_mode(self):
        self.w.sys.history.inject_failure(True)
        self.w.sys.history.append(Category.SYSTEM, "p", {})
        self.assertIs(self.w.sys.mode.mode(), SysMode.FAULT)
        self.assertFalse(self.ac.create_user(self.w.auth("admin1", Fn.ADMIN), "x1", Role.NURSE, "long-enough-1").ok)

    def test_bootstrap_works_only_when_no_account_exists(self):
        self.assertFalse(self.ac.bootstrap_administrator("admin2", "long-enough-1").ok)

    def test_passwords_are_never_stored_in_clear_or_returned(self):
        self.assertNotIn("nurse1-password", str(self.w.sys.history.query()))
        raw = [s for _, s in self.w.sys.history.replay() if s]
        self.assertTrue(all("nurse1-password" not in str(x) for x in raw))
        self.assertNotIn("password", str(self.ac.list_users()).lower().replace("disabled", ""))

    def test_NFR_AUD_03_every_login_attempt_is_recorded(self):
        self.ac.login("nobody", "whatever-1")
        self.ac.login("nurse1", "wrong-password")
        self.ac.login("nurse1", "nurse1-password")
        kinds = [e.kind for e in self.w.sys.history.query(category=Category.ACCESS, user_id="nurse1")]
        self.assertEqual(kinds[-2:], ["login_failed", "login_ok"])
        self.assertTrue(self.w.sys.history.query(kind="login_failed", user_id="nobody"))


if __name__ == "__main__":
    unittest.main()
