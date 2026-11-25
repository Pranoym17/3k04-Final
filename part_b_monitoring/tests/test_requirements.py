"""One named test for each D1 requirement that no other test file names directly.

Together with the other files, every one of the 71 requirement IDs has a test whose
name contains the ID (checked by the traceability script). Timing tests run the
whole system in one process, so they measure processing time, not network delay."""
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from helpers import *                                    # noqa: F401,F403
from monitoring.domain import Category, ChangeKind, Fn, SysMode

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PW = {"admin1": "admin-pass-1", "pharm1": "pharm1-password", "nurse1": "nurse1-password", "tech1": "tech1-password"}


def web(w, uid=None):
    app = make_app(w.sys)
    app.testing = True
    c = app.test_client()
    if uid:
        assert c.post("/login", data={"user_id": uid, "password": PW[uid]}).status_code == 302
    return c


def fifty(w):
    adcs = [w.add_adc(f"ADC-{i:02d}", slots=((1, "Morphine", 20), (2, "Heparin", 3))) for i in range(1, 51)]
    for a in adcs:
        w.online(a)
    return adcs


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.adc = self.w.add_adc()
        self.w.online(self.adc)

    # ---- communication
    def test_FR_COM_06_a_new_threshold_shows_as_unconfirmed_until_the_adc_acknowledges(self):
        c = self.w.sys.config.request_threshold_change(self.w.auth("pharm1", Fn.CHANGE_THRESHOLD), "ADC-01", 3, 8).value
        page = web(self.w, "pharm1").get("/adc/ADC-01").get_data(as_text=True)
        self.assertIn("<strong>Unconfirmed</strong>", page)
        self.adc.ack(c.config_id, 3, 8)
        after = web(self.w, "pharm1").get("/adc/ADC-01").get_data(as_text=True)
        self.assertNotIn("<strong>Unconfirmed</strong>", after)
        self.assertIn("<strong>Confirmed</strong>", after)

    def test_FR_COM_08_a_threshold_change_for_an_adc_that_is_not_online_is_refused_with_the_reason(self):
        self.w.advance(31)
        client = web(self.w, "pharm1")
        page = client.post("/adc/ADC-01/threshold", data={"slot": 3, "value": 8}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Offline", page)
        self.assertEqual(self.w.sys.config.changes("ADC-01"), [])

    # ---- history
    def test_FR_HIS_03_connection_alarm_escalation_and_reply_events_are_all_recorded(self):
        self.adc.alarm("A-1")
        self.adc.slots[3][1] = 1
        self.adc.inventory()
        self.adc.request("R-1", "refill", slots=[3])
        self.w.advance(125); self.adc.status()
        kinds = {e.kind for e in self.w.sys.history.query(adc_id="ADC-01")}
        self.assertTrue({"connection_change", "alarm_change", "escalation", "request_reply"} <= kinds, kinds)

    def test_FR_HIS_05_an_event_that_is_invalid_in_the_current_state_is_ignored_and_recorded(self):
        self.adc.alarm("A-1")
        self.adc.alarm("A-1")                                              # same notice again
        self.adc.clear("A-1"); self.adc.clear("A-1")                       # clear twice
        ignored = self.w.sys.history.query(kind="ignored_event", adc_id="ADC-01")
        self.assertGreaterEqual(len(ignored), 2)
        self.assertEqual(self.w.sys.alarms.get("ADC-01/A-1").state.name, "CLEARED")

    def test_FR_HIS_06_an_administrator_retrieves_one_users_actions_in_a_time_range(self):
        t0 = self.w.clock.now()
        self.w.sys.config.request_threshold_change(self.w.auth("pharm1", Fn.CHANGE_THRESHOLD), "ADC-01", 3, 8)
        self.w.clock.advance(100)
        self.w.sys.alarms.acknowledge(self.w.auth("tech1", Fn.ACK_ALARM), "ADC-01/none")
        page = web(self.w, "admin1").get("/admin/audit?user_id=pharm1").get_data(as_text=True)
        self.assertIn("threshold_change", page)
        mine = self.w.sys.history.query(category=Category.USER_ACTION, user_id="pharm1", start=t0 - 1, end=t0 + 50)
        self.assertEqual(len(mine), 1)
        self.assertEqual(self.w.sys.history.query(category=Category.USER_ACTION, user_id="pharm1", start=t0 + 60), [])

    # ---- dashboard
    def test_FR_DSH_01_one_dashboard_row_per_adc_with_five_facts(self):
        self.adc.slots[4][1] = 2
        self.adc.inventory()
        self.adc.alarm("A-1")
        page = web(self.w, "nurse1").get("/dashboard/live").get_data(as_text=True)
        row = re.search(r"<tr>\s*<th scope=\"row\"><a[^>]*>ADC-01</a></th>(.*?)</tr>", page, re.S).group(1)
        self.assertIn("Online", row)                                       # connection status
        self.assertRegex(row, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")           # last valid message time
        self.assertIn("Normal", row)                                       # mode
        self.assertRegex(row, r"<td>1</td>\s*<td>1</td>")                  # active alarms, Low slots

    # ---- request handling
    def test_FR_REQ_02_a_request_naming_a_slot_unknown_for_that_adc_is_rejected(self):
        self.adc.request("R-1", "refill", slots=[42])
        r = self.adc.got("request_reply")[-1]
        self.assertEqual((r["decision"], r["reason"]), ("reject", "unknown slot"))

    def test_FR_REQ_03_a_refill_is_accepted_only_for_a_slot_flagged_low(self):
        self.adc.slots[3][1] = 5
        self.adc.inventory()
        self.adc.request("R-1", "refill", slots=[3, 4])
        got = {r["slot"]: r["decision"] for r in self.adc.got("request_reply")}
        self.assertEqual(got, {3: "accept", 4: "reject"})

    # ---- modes
    def test_FR_MOD_02_a_failed_write_to_the_history_store_enters_fault_mode(self):
        self.w.sys.history.inject_failure(True)
        self.w.sys.history.append(Category.SYSTEM, "probe", {})
        self.assertIs(self.w.sys.mode.mode(), SysMode.FAULT)
        self.assertFalse(self.w.sys.mode.allows(ChangeKind.THRESHOLD).ok)

    # ---- non-functional
    def test_NFR_SAF_01_every_inventory_value_shows_the_time_its_adc_reported_it(self):
        page = web(self.w, "nurse1").get("/adc/ADC-01").get_data(as_text=True)
        rows = re.findall(r"<tr>.*?</tr>", page, re.S)
        slot_rows = [r for r in rows if "Morphine" in r or "Heparin" in r or "Saline" in r]
        self.assertEqual(len(slot_rows), 3)
        for r in slot_rows:
            self.assertRegex(r, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")

    def test_NFR_SEC_04_adc_and_workstation_traffic_runs_over_tls_when_the_tls_option_is_on(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl is not installed")
        d = tempfile.mkdtemp()
        cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "2", "-subj", "/CN=localhost"], check=True, capture_output=True)
        from werkzeug.serving import make_server
        from adc_simulator.sim import SimAdc
        from monitoring.clock import SystemClock
        live = World(clock=SystemClock())                                  # real time: the stand-in stamps real time
        app = make_app(live.sys)
        srv = make_server("127.0.0.1", 0, app, threaded=True, ssl_context=(cert, key))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            live.add_adc("ADC-09", key="demo-key-ADC-09")
            sim = SimAdc("ADC-09", "demo-key-ADC-09", f"https://127.0.0.1:{srv.server_port}", insecure=True)
            self.assertEqual(sim.send_status()["result"], "accepted")
            plain = SimAdc("ADC-09", "demo-key-ADC-09", f"http://127.0.0.1:{srv.server_port}")
            self.assertIsNone(plain.send_status())                         # plain HTTP is not understood
        finally:
            srv.shutdown()

    def test_NFR_PRF_01_an_alarm_is_on_the_dashboard_within_5_s_while_50_adcs_report(self):
        w = World()
        adcs = fifty(w)
        client = web(w, "pharm1")
        t0 = time.perf_counter()
        for a in adcs:                                                     # one normal round of reports
            a.status(); a.inventory()
        adcs[7].alarm("A-99", "door open", "high")
        page = client.get("/dashboard/live").get_data(as_text=True)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertIn("A-99", page)

    def test_NFR_PRF_02_a_refill_request_is_answered_within_5_s_including_an_unknown_slot(self):
        w = World()
        adcs = fifty(w)
        t0 = time.perf_counter()
        ans = adcs[0].request("R-1", "refill", slots=[2, 77])
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual({r["slot"] for r in adcs[0].got("request_reply")}, {2, 77})

    def test_NFR_PRF_04_the_dashboard_shows_a_status_or_inventory_message_within_5_s(self):
        client = web(self.w, "nurse1")
        t0 = time.perf_counter()
        self.adc.status("Fault")
        self.adc.slots[1][1] = 1
        self.adc.inventory()
        page = client.get("/dashboard/live").get_data(as_text=True)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertIn("Fault", page)
        self.assertIn("<td>1</td>", page)

    def test_NFR_AVL_01_an_uptime_log_line_is_written_every_minute(self):
        d = tempfile.mkdtemp()
        w = World(os.path.join(d, "h.db"))
        w.advance(300, step=5)
        lines = open(os.path.join(d, "uptime.log")).read().splitlines()
        self.assertGreaterEqual(len(lines), 5)
        self.assertTrue(all(re.match(r"\d+ alive mode=\w+$", ln) for ln in lines))
        gaps = [int(b.split()[0]) - int(a.split()[0]) for a, b in zip(lines, lines[1:])]
        self.assertTrue(all(55 <= g <= 65 for g in gaps), gaps)

    def test_NFR_USA_01_every_alarm_and_adc_mode_has_a_text_label_beside_its_colour(self):
        self.adc.alarm("A-1", severity="high"); self.adc.alarm("A-2", severity="medium"); self.adc.alarm("A-3", severity="low")
        self.adc.status("Fault")
        page = web(self.w, "nurse1").get("/dashboard/live").get_data(as_text=True)
        for label in ("High", "Medium", "Low", "Fault", "Online"):
            self.assertRegex(page, rf"<span class=\"(?:sev|chip)[^\"]*\">[^<]*{label}")
        self.assertRegex(page, r"(!!!|&#9650;)")                           # a symbol as well as colour and text

    def test_NFR_USA_02_every_function_is_a_native_link_button_or_field(self):
        tdir = os.path.join(ROOT, "monitoring", "web", "templates")
        for name in os.listdir(tdir):
            src = open(os.path.join(tdir, name), encoding="utf-8").read()
            self.assertNotRegex(src, r"\bonclick\s*=", name)
            self.assertNotRegex(src, r"<(div|span)[^>]*\brole=\"button\"", name)
            self.assertNotRegex(src, r"tabindex=\"[1-9]", name)
        js = open(os.path.join(ROOT, "monitoring", "web", "static", "live.js"), encoding="utf-8").read()
        self.assertNotRegex(js, r"mouse(over|down|up|enter|leave)|dblclick|contextmenu")
        css = open(os.path.join(ROOT, "monitoring", "web", "static", "style.css"), encoding="utf-8").read()
        self.assertIn(":focus", css)                                       # focus is visible

    def test_NFR_USA_03_a_pharmacist_reaches_the_list_of_every_low_slot_in_three_requests(self):
        w = World()
        adcs = fifty(w)
        client = web(w, "pharm1")                                          # request 1: login
        t0 = time.perf_counter()
        client.get("/reports")                                             # request 2: the reports page
        page = client.get("/reports/inventory").get_data(as_text=True)     # request 3: the Low-slot report
        self.assertLess(time.perf_counter() - t0, 60.0)
        self.assertEqual(page.count("Heparin"), 50)                        # one Low slot per ADC


if __name__ == "__main__":
    unittest.main()
