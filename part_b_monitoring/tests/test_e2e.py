"""End to end over real HTTP: the Monitoring System, a real server thread and the ADC stand-in at 10x speed."""
import os
import tempfile
import threading
import time
import unittest

from helpers import *                                    # noqa: F401,F403
from werkzeug.serving import make_server

from adc_simulator.sim import SimAdc
from monitoring.domain import ConnState, Fn
from monitoring.policy import Policy
from monitoring.seed import seed
from monitoring.system import MonitoringSystem

SCALE = 10.0


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db = os.path.join(tempfile.mkdtemp(), "h.db")
        cls.system = MonitoringSystem(db, policy=Policy().scaled(SCALE))
        cls.system.start()
        seed(cls.system)
        cls.system.run_ticker()
        cls.server = make_server("127.0.0.1", 0, make_app(cls.system), threaded=True)
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.adc = SimAdc("ADC-01", "demo-key-ADC-01", f"http://127.0.0.1:{cls.port}",
                         interval=10.0 / SCALE, inventory_every=60.0 / SCALE)
        cls.adc.start()

    @classmethod
    def tearDownClass(cls):
        cls.adc.stop()
        cls.system.stop()
        cls.server.shutdown()

    def state(self):
        with self.system.lock:
            return self.system.health.state_of("ADC-01")

    def test_1_adc_comes_online_over_http(self):
        self.assertTrue(wait_for(lambda: self.state() is ConnState.ONLINE))

    def test_2_silence_then_recovery_with_alarm_raised_and_cleared(self):
        self.assertTrue(wait_for(lambda: self.state() is ConnState.ONLINE))
        self.adc.silence()
        self.assertTrue(wait_for(lambda: self.state() is ConnState.OFFLINE))
        with self.system.lock:
            self.assertTrue(any(a.alarm_type == "communication" and a.adc_id == "ADC-01" for a in self.system.alarms.active_alarms()))
        self.adc.resume()
        self.assertTrue(wait_for(lambda: self.state() is ConnState.ONLINE))
        with self.system.lock:
            self.assertFalse(any(a.alarm_type == "communication" and a.adc_id == "ADC-01" for a in self.system.alarms.active_alarms()))

    def test_3_threshold_change_reaches_the_adc_and_is_confirmed(self):
        self.assertTrue(wait_for(lambda: self.state() is ConnState.ONLINE))
        with self.system.lock:
            tok = self.system.access.login("pharm1", "12345").value
            res = self.system.config.request_threshold_change(
                self.system.access.authorise(tok, Fn.CHANGE_THRESHOLD), "ADC-01", 3, 9)
        self.assertTrue(res.ok, res.reason)
        cid = res.value.config_id
        def confirmed():
            with self.system.lock:
                return self.system.config.changes("ADC-01")[-1].status == "Confirmed"
        self.assertTrue(wait_for(confirmed))
        with self.system.lock:
            self.assertEqual(self.system.inventory.confirmed_threshold("ADC-01", 3), 9)

    def test_4_alarm_travels_from_the_adc_to_the_dashboard_list(self):
        self.assertTrue(wait_for(lambda: self.state() is ConnState.ONLINE))
        self.adc.raise_alarm("door open", "high")
        key = f"ADC-01/{self.adc.last_alarm}"
        def shown():
            with self.system.lock:
                return key in [a.key for a in self.system.alarms.active_alarms()]
        self.assertTrue(wait_for(shown))
        self.adc.clear_alarm()
        self.assertTrue(wait_for(lambda: not shown()))

    def test_5_the_bad_message_buttons_are_all_rejected(self):
        self.assertTrue(wait_for(lambda: self.state() is ConnState.ONLINE))
        for action in (self.adc.bad_auth, self.adc.malformed, self.adc.unregistered, self.adc.patient_field):
            ans = action()
            self.assertEqual(ans["result"], "rejected", action.__name__)
        self.assertEqual(self.adc.duplicate()["result"], "rejected")   # last raw was the patient message: still invalid


if __name__ == "__main__":
    unittest.main()
