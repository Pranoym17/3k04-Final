"""Live test: the real run_monitoring.py process over HTTPS, the real Part A
application and the adapter's HttpTransport. Proves the TLS transport and the
signing rule agree outside the test client. Slower (about 5 s) because it
starts a server and may generate a certificate."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (os.path.join(ROOT, "part_a_adc"), os.path.join(ROOT, "integration")):
    if p not in sys.path:
        sys.path.insert(0, p)

from adc.app import AdcApplication                                   # noqa: E402
from adc.hardware.clock import SimulatedClock                        # noqa: E402
from adc.hardware.credential_reader import SimulatedCredentialReader  # noqa: E402
from adc.hardware.drawer_sensor import SimulatedDrawerSensor         # noqa: E402
from adc.hardware.lock_driver import SimulatedLockDriver             # noqa: E402
from adc.hardware.removal_sensor import SimulatedRemovalSensor       # noqa: E402
from adc.main import DRAWERS, INACTIVE, ITEMS, ROLES, TOKENS         # noqa: E402
from adc_bridge import HttpTransport, MonitoringLink                 # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveHttpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.db = os.path.join(cls.dir, "history.db")
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, "-W", "ignore", os.path.join(ROOT, "part_b_monitoring", "run_monitoring.py"),
             "--fresh", "--seed", "--db", cls.db, "--port", str(cls.port)],
            cwd=os.path.join(ROOT, "part_b_monitoring"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        end = time.time() + 30
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.5).close()
                return
            except OSError:
                if cls.proc.poll() is not None:
                    break
                time.sleep(0.2)
        out = cls.proc.stdout.read().decode(errors="replace") if cls.proc.poll() is not None else ""
        cls.proc.kill()
        raise unittest.SkipTest("Monitoring System did not start: " + out[-400:])

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def test_L01_part_a_reaches_part_b_over_https_and_a_dispense_is_recorded(self):
        clock = SimulatedClock()
        removal, drawers = SimulatedRemovalSensor(), SimulatedDrawerSensor(DRAWERS)
        adc = AdcApplication("ADC-01", clock, SimulatedLockDriver(DRAWERS), drawers,
                             SimulatedCredentialReader(TOKENS), removal, DRAWERS, ITEMS, ROLES, INACTIVE)
        adc.start_up()
        link = MonitoringLink(adc, "ADC-01", "demo-key-ADC-01",
                              HttpTransport(f"https://127.0.0.1:{self.port}", insecure=True))
        self.assertEqual(link.pump(), "up", link.log)
        adc.authenticate("card-nurse")
        adc.request_dispense("MORPH-10MG", 2)
        adc.confirm_dispense()
        for d in DRAWERS:
            removal.set_confirmed(d, True)
            drawers.set_closed(d)
        adc.close_drawer_and_complete()
        self.assertEqual(link.pump(), "up", link.log)
        self.assertEqual(adc.pending_messages(), [])
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        rows = [json.loads(d) for (d,) in con.execute(
            "SELECT data FROM events WHERE category='message' AND adc_id='ADC-01' ORDER BY seq")]
        con.close()
        kinds = [(r["msg_type"], r["outcome"]) for r in rows]
        self.assertIn(("status", "Accepted"), kinds)
        self.assertIn(("dispensing", "Accepted"), kinds)
        self.assertIn(("inventory", "Accepted"), kinds)

    def test_L03_threshold_set_on_the_dashboard_is_confirmed_by_the_adc(self):
        import http.cookiejar
        import ssl
        import urllib.parse
        import urllib.request
        adc = AdcApplication("ADC-01", SimulatedClock(), SimulatedLockDriver(DRAWERS),
                             SimulatedDrawerSensor(DRAWERS), SimulatedCredentialReader(TOKENS),
                             SimulatedRemovalSensor(), DRAWERS, ITEMS, ROLES, INACTIVE)
        adc.start_up()
        link = MonitoringLink(adc, "ADC-01", "demo-key-ADC-01",
                              HttpTransport(f"https://127.0.0.1:{self.port}", insecure=True))
        self.assertEqual(link.pump(), "up", link.log)          # ADC-01 Online, slots known
        base = f"https://127.0.0.1:{self.port}"
        browser = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        form = lambda d: urllib.parse.urlencode(d).encode()
        browser.open(base + "/login", form({"user_id": "pharm1", "password": "12345"})).read()
        page = browser.open(base + "/adc/ADC-01/threshold", form({"slot": "1", "value": "11"})).read().decode()
        self.assertIn("Unconfirmed", page)
        link.pump()                                            # outbox -> CONFIG -> config_ack
        page = browser.open(base + "/adc/ADC-01").read().decode()
        self.assertIn("Confirmed", page)
        self.assertEqual(link.thresholds, {1: 11})
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        acks = [json.loads(d) for (d,) in con.execute(
            "SELECT data FROM events WHERE category='message' AND adc_id='ADC-01'")]
        con.close()
        self.assertIn(("config_ack", "Accepted"), [(r["msg_type"], r["outcome"]) for r in acks])

    def test_L02_plain_http_to_the_https_server_counts_as_link_down(self):
        adc = AdcApplication("ADC-01", SimulatedClock(), SimulatedLockDriver(DRAWERS),
                             SimulatedDrawerSensor(DRAWERS), SimulatedCredentialReader(TOKENS),
                             SimulatedRemovalSensor(), DRAWERS, ITEMS, ROLES, INACTIVE)
        adc.start_up()
        link = MonitoringLink(adc, "ADC-01", "demo-key-ADC-01",
                              HttpTransport(f"http://127.0.0.1:{self.port}"))
        self.assertEqual(link.pump(), "down")
        self.assertEqual(len(adc.pending_messages()), 1)


if __name__ == "__main__":
    unittest.main()
