"""Regression checks for defects found in the final checklist audit."""
import os
import hashlib
import hmac
import json
import tempfile
import unittest
import threading
import time
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from helpers import World
from monitoring.policy import Policy
from monitoring.domain import AdcMode, AlarmState, Category, ConnState, FailSafe, Fn, MsgKind, Origin, OutInventoryRequest
from monitoring.health_monitor import ConnEvent
from monitoring.transition_table import ILLEGAL, Illegal, TransitionTable
from monitoring.system import MonitoringSystem
from monitoring.web.dashboard import create_app


class AuditTests(unittest.TestCase):
    def test_NFR_SEC_04_the_launch_command_has_no_plaintext_override(self):
        result = subprocess.run([sys.executable, "run_monitoring.py", "--http"],
                                cwd=os.path.dirname(os.path.dirname(__file__)),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --http", result.stderr)

    def test_NFR_USA_01_all_adc_modes_have_text_and_colour_on_both_views(self):
        world = World()
        adc = world.add_adc()
        world.online(adc)
        client = create_app(world.sys).test_client()
        client.post("/login", data={"user_id": "nurse1", "password": "nurse1-password"})
        for mode in ("Normal", "Maintenance", "Fault"):
            adc.status(mode)
            for path in ("/dashboard/live", "/adc/ADC-01"):
                page = client.get(path).get_data(as_text=True)
                self.assertRegex(page, f'class="chip chip-{mode.lower()}">(?:&#9650; )?{mode}')

    def test_NFR_SAF_01_finite_adc_times_outside_platform_date_range_still_display(self):
        world = World()
        adc = world.add_adc()
        world.online(adc)
        adc.alarm("FUTURE", ts=1e308)
        adc.inventory(ts=1e308)
        app = create_app(world.sys)
        app.testing = True
        client = app.test_client()
        client.post("/login", data={"user_id": "nurse1", "password": "nurse1-password"})
        for path in ("/dashboard", "/adc/ADC-01"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("1e+308", response.get_data(as_text=True))

    def test_FR_COM_10_restart_preserves_the_designed_message_id_reset_on_reregistration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.db")
            world = World(path)
            adc = world.add_adc()
            world.online(adc)
            adc.status(mid=100)
            administrator = world.auth("admin1", Fn.ADMIN)
            world.sys.config.deregister_adc(administrator, adc.id)
            world.sys.config.register_adc(administrator, adc.id, adc.key)
            adc.status(mid=1)
            restored = MonitoringSystem(path, clock=world.clock)
            try:
                restored.start()
                restored.handle_adc_bytes(adc.raw(adc.build("status", mid=100, mode="Normal")))
                self.assertEqual(restored.history.query(kind="adc_message")[-1].data["outcome"], "Accepted")
            finally:
                restored.history._db.close()
                world.sys.history._db.close()

    def test_NFR_PRF_03_fifty_concurrent_adcs_record_one_hundred_messages_within_five_seconds(self):
        world = World()
        adcs = [world.add_adc(f"ADC-{index:02d}") for index in range(1, 51)]
        barrier = threading.Barrier(50)
        def send(adc):
            barrier.wait(timeout=5)
            return adc.status(), adc.inventory()
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(send, adcs))
        elapsed = time.perf_counter() - started
        self.assertTrue(all(response["result"] == "accepted" for pair in results for response in pair))
        self.assertEqual(len(world.sys.history.query(kind="adc_message")), 100)
        self.assertLess(elapsed, 5)

    def test_NFR_AUD_03_invalid_forms_reports_and_demo_attempts_are_recorded(self):
        world = World()
        adc = world.add_adc()
        adc.request("MAINT-1", "maintenance")
        client = create_app(world.sys).test_client()
        client.post("/login", data={"user_id": "tech1", "password": "tech1-password"})
        client.post("/requests/decide", data={"key": "ADC-01/MAINT-1", "decision": "unknown"})
        self.assertEqual(len(world.sys.requests.pending_maintenance()), 1)
        self.assertEqual(world.sys.history.query(kind="action_refused")[-1].user_id, "tech1")
        report_auth = world.auth("tech1", Fn.RUN_ALARM_AVAIL_REPORTS)
        self.assertFalse(world.sys.reports.alarm_report(report_auth, 2, 1).ok)
        client.get("/reports/alarms?start=invalid")
        self.assertEqual(len(world.sys.history.query(kind="action_refused", user_id="tech1")), 3)
        administrator = world.auth("admin1", Fn.ADMIN)
        world.sys.mode.switch_to_maintenance(administrator)
        client.post("/login", data={"user_id": "admin1", "password": "admin-pass-1"})
        client.post("/admin/mode", data={"mode": "unknown"})
        self.assertEqual(world.sys.mode.mode().value, "Maintenance")
        world.sys._demo_login("unknown-demo-user")
        self.assertEqual(world.sys.history.query(kind="login_failed")[-1].user_id, "unknown-demo-user")

    def test_NFR_SEC_02_rejected_metadata_cannot_store_nested_patient_content(self):
        world = World()
        adc = world.add_adc()
        for field in ("type", "message_id", "timestamp"):
            message = adc.build("status", mode="Normal")
            message[field] = {"patient_id": "AUDIT-PRIVATE"}
            self.assertEqual(adc.send_fields(message)["result"], "rejected")
        message = adc.build("status", mode="Normal")
        message["patient_id_AUDIT-PRIVATE"] = "AUDIT-PRIVATE"
        self.assertEqual(adc.send_fields(message)["result"], "rejected")
        self.assertNotIn("AUDIT-PRIVATE", json.dumps([event.data for event in world.sys.history.query()]))

    def test_FR_COM_01_invalid_registry_ids_are_refused_without_raising(self):
        world = World()
        administrator = world.auth("admin1", Fn.ADMIN)
        for adc_id in ("ADC-01\n", 123, None, ""):
            self.assertFalse(world.sys.config.register_adc(administrator, adc_id).ok)
        self.assertEqual(world.sys.config.registered_ids(), [])

    def test_FR_COM_09_FR_COM_10_nonrejected_responses_do_not_identify_validation_rows(self):
        world = World()
        adc = world.add_adc()
        adc.status()
        adc.inventory()
        message = adc.build("dispensing", slot=1, medication="Morphine", quantity=1, adc_user_id="RN-7")
        accepted = adc.send_fields(message)
        duplicate = adc.send_fields(message)
        late = adc.send("dispensing", ts=world.clock.now() - 31, slot=1,
                        medication="Morphine", quantity=1, adc_user_id="RN-7")
        self.assertEqual(accepted, duplicate)
        self.assertEqual(accepted, late)
        outcomes = [event.data["outcome"] for event in world.sys.history.query(kind="adc_message")[-3:]]
        self.assertEqual(outcomes, ["Accepted", "Duplicate", "Late"])

    def test_FR_DSH_01_last_valid_time_updates_without_extending_reconnection(self):
        world = World()
        adc = world.add_adc()
        adc.status()
        world.clock.advance(10)
        adc.status()
        self.assertEqual(world.sys.health.snapshot(adc.id).last_valid_receive, world.clock.now())
        world.clock.advance(20)
        world.sys.tick()
        self.assertIs(world.sys.health.state_of(adc.id), ConnState.OFFLINE)
        world.clock.advance(1)
        adc.inventory()
        snapshot = world.sys.health.snapshot(adc.id)
        self.assertIs(snapshot.state, ConnState.OFFLINE)
        self.assertEqual(snapshot.last_valid_receive, world.clock.now())

    def test_NFR_REL_01_adc_alarm_ids_cannot_break_replay_or_be_overwritten_by_monitoring(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.db")
            world = World(path)
            adc = world.add_adc()
            world.online(adc)
            adc.alarm("MS-ABCDE")
            adc.alarm("MS-00002")
            world.advance(world.sys.policy.offline_timeout_s)
            original = world.sys.alarms.get("ADC-01/MS-00002")
            self.assertIs(original.origin, Origin.ADC)
            self.assertIs(original.state, AlarmState.RAISED)
            restored = MonitoringSystem(path, clock=world.clock)
            try:
                restored.start()
                for alarm_id in ("MS-ABCDE", "MS-00002"):
                    self.assertIs(restored.alarms.get(f"ADC-01/{alarm_id}").origin, Origin.ADC)
            finally:
                restored.history._db.close()
                world.sys.history._db.close()

    def test_FR_REQ_05_an_overdue_decision_cannot_win_before_the_next_tick(self):
        world = World()
        adc = world.add_adc()
        adc.request("MAINT-1", "maintenance")
        world.clock.advance(world.sys.policy.maintenance_decision_timeout_s)
        technician = world.auth("tech1", Fn.DECIDE_MAINTENANCE)
        self.assertFalse(world.sys.requests.decide_maintenance(technician, "ADC-01/MAINT-1", True).ok)
        self.assertFalse(world.sys.requests.decide_maintenance(technician, "ADC-01/MAINT-1", False).ok)
        replies = world.sys.history.query(kind="request_reply")
        self.assertEqual(len(replies), 1)
        self.assertEqual((replies[0].data["decision"], replies[0].data["reason"]), ("reject", "no decision"))
        self.assertEqual(world.sys.history.query(kind="maintenance_decided"), [])

    def test_state_tables_reject_blank_and_invalid_cells_and_normalise_illegal(self):
        for blank in (None, object()):
            with self.assertRaises(ValueError):
                TransitionTable("audit", [ConnState.OFFLINE], [ConnEvent.STATUS],
                                {ConnState.OFFLINE: {ConnEvent.STATUS: blank}})
        table = TransitionTable("audit", [ConnState.OFFLINE], [ConnEvent.STATUS],
                                {ConnState.OFFLINE: {ConnEvent.STATUS: Illegal()}})
        self.assertIs(table.lookup(ConnState.OFFLINE, ConnEvent.STATUS), ILLEGAL)
        self.assertIs(table.lookup(ConnState.ONLINE, ConnEvent.STATUS), ILLEGAL)

    def test_invariant_failures_escape_message_dispatch_and_unknown_actions_fail_safe(self):
        world = World()
        adc = world.add_adc()
        def violated(message):
            raise FailSafe("AUDIT-INVARIANT")
        world.sys.comm.register_handler(MsgKind.STATUS, violated)
        with self.assertRaises(FailSafe):
            adc.status()
        with self.assertRaises(FailSafe):
            world.sys.comm._encode(adc.id, object())
        with self.assertRaises(FailSafe):
            world.sys.mode._dispatch("unknown", "audit")
        with self.assertRaises(FailSafe):
            world.sys.access.authorise(world.tokens["admin1"], "unknown")

    def test_NFR_SEC_04_https_login_marks_the_session_cookie_secure(self):
        world = World()
        client = create_app(world.sys).test_client()
        response = client.post("/login", data={"user_id": "nurse1", "password": "nurse1-password"},
                               base_url="https://localhost")
        cookie = next(value for value in response.headers.getlist("Set-Cookie") if value.startswith("ms_session="))
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_NFR_SEC_01_automatic_live_polls_do_not_extend_idle_sessions(self):
        world = World()
        client = create_app(world.sys).test_client()
        client.post("/login", data={"user_id": "nurse1", "password": "nurse1-password"})
        for _ in range(2):
            world.clock.advance(299)
            self.assertEqual(client.get("/dashboard/live").status_code, 200)
        world.clock.advance(2)
        self.assertEqual(client.get("/dashboard/live").status_code, 302)

    def test_FR_MOD_03_live_responses_carry_fault_banner_and_recovery(self):
        world = World()
        client = create_app(world.sys).test_client()
        client.post("/login", data={"user_id": "nurse1", "password": "nurse1-password"})
        self.assertIn('id="mode-banner"', client.get("/dashboard").get_data(as_text=True))
        world.sys.history.inject_failure(True)
        world.sys.history.append(Category.SYSTEM, "audit_storage_failure")
        response = client.get("/dashboard/live")
        self.assertEqual(response.headers["X-Monitoring-Mode"], "Fault")
        self.assertIn("FAULT", response.headers["X-Monitoring-Banner"])
        world.sys.history.inject_failure(False)
        world.clock.advance(world.sys.policy.fault_test_interval_s)
        world.sys.tick()
        self.assertEqual(client.get("/dashboard/live").headers["X-Monitoring-Banner"], "")

    def test_FR_AUT_02_controls_follow_permissions_and_direct_posts_still_refuse(self):
        world = World()
        adc = world.add_adc()
        adc.alarm("ALARM-1")
        app = create_app(world.sys)
        app.testing = True
        for user_id in ("nurse1", "admin1", "pharm1", "tech1"):
            client = app.test_client()
            password = "admin-pass-1" if user_id == "admin1" else f"{user_id}-password"
            client.post("/login", data={"user_id": user_id, "password": password})
            dashboard = client.get("/dashboard").get_data(as_text=True)
            cabinet = client.get(f"/adc/{adc.id}").get_data(as_text=True)
            self.assertEqual('action="/alarms/ack"' in dashboard, user_id in ("pharm1", "tech1"))
            self.assertEqual('id="tone-toggle"' in dashboard, user_id in ("pharm1", "tech1"))
            self.assertEqual('action="/adc/ADC-01/threshold"' in cabinet, user_id == "pharm1")
            self.assertEqual('href="/admin"' in dashboard, user_id == "admin1")
            self.assertEqual('href="/history"' in dashboard, user_id != "nurse1")
            if user_id in ("nurse1", "admin1"):
                self.assertEqual(client.post("/alarms/ack", data={"key": "ADC-01/ALARM-1"}).status_code, 403)

    def test_NFR_AUD_03_module_refusals_record_actor_and_time_without_changing_data(self):
        world = World()
        adc = world.add_adc()
        world.online(adc)
        admin = world.auth("admin1", Fn.ADMIN)
        pharmacist = world.auth("pharm1", Fn.CHANGE_THRESHOLD)
        technician = world.auth("tech1", Fn.DECIDE_MAINTENANCE)
        acknowledge = world.auth("tech1", Fn.ACK_ALARM)
        actions = (
            ("admin1", lambda: world.sys.config.register_adc(admin, adc.id)),
            ("admin1", lambda: world.sys.access.disable_user(admin, "missing")),
            ("admin1", lambda: world.sys.access.create_user(admin, "bad user", admin.role, "password")),
            ("pharm1", lambda: world.sys.config.request_threshold_change(pharmacist, adc.id, 99, 8)),
            ("tech1", lambda: world.sys.requests.decide_maintenance(technician, "missing", True)),
            ("tech1", lambda: world.sys.alarms.acknowledge(acknowledge, "missing")),
            ("pharm1", lambda: world.sys.mode.switch_to_maintenance(pharmacist)),
        )
        for user_id, action in actions:
            with self.subTest(user_id=user_id, action=action):
                before = len(world.sys.history.query(category=Category.USER_ACTION))
                self.assertFalse(action().ok)
                event = world.sys.history.query(kind="action_refused")[-1]
                self.assertEqual((event.user_id, event.ts), (user_id, world.clock.now()))
                self.assertEqual(len(world.sys.history.query(category=Category.USER_ACTION)), before)
        world.sys.mode.switch_to_maintenance(admin)
        self.assertFalse(world.sys.config.request_threshold_change(pharmacist, adc.id, 3, 8).ok)
        self.assertIn("Maintenance", world.sys.history.query(kind="action_refused")[-1].data["reason"])

    def test_NFR_REL_01_replay_matches_live_inventory_and_connection_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.db")
            world = World(path)
            adc = world.add_adc()
            world.online(adc)
            adc.status("Maintenance")
            world.clock.advance(1)
            adc.slots[3][1] = 4
            adc.inventory()
            adc.slots[3][1] = 80
            adc.inventory(ts=world.clock.now() - 1)
            expected = world.sys.inventory.slots(adc.id)
            received = world.sys.health.snapshot(adc.id).last_valid_receive
            restored = MonitoringSystem(path, clock=world.clock)
            try:
                restored.start()
                self.assertEqual(restored.inventory.slots(adc.id), expected)
                snapshot = restored.health.snapshot(adc.id)
                self.assertIs(snapshot.state, ConnState.OFFLINE)
                self.assertIs(snapshot.adc_mode, AdcMode.MAINTENANCE)
                self.assertEqual(snapshot.last_valid_receive, received)
                world.sys.config.deregister_adc(world.auth("admin1", Fn.ADMIN), adc.id)
                restored.inventory.restore(world.sys.history.replay())
                self.assertEqual(restored.inventory.slots(adc.id), [])
            finally:
                restored.history._db.close()
                world.sys.history._db.close()

    def test_FR_COM_04_validation_diagnostics_stay_in_history(self):
        world = World()
        adc = world.add_adc()
        answers = [json.loads(world.sys.handle_adc_bytes(b"not json")),
                   adc.status(bad_auth=True), adc.send("status", mode="Broken")]
        unknown = adc.build("status", mode="Normal")
        unknown["adc_id"] = "ADC-99"
        answers.append(adc.send_fields(unknown))
        self.assertTrue(all(answer == answers[0] for answer in answers))
        reasons = {event.data["reason"] for event in world.sys.history.query(kind="adc_message")}
        self.assertEqual(len(reasons), 4)

    def test_FR_COM_04_malformed_authenticated_values_are_rejected_once(self):
        world = World()
        adc = world.add_adc()
        for fields in ({"type": []}, {"type": {}}, {"timestamp": float("nan")},
                       {"timestamp": float("inf")}, {"timestamp": 10**400},
                       {"mode": "Normal\n"}):
            with self.subTest(fields=fields):
                message = adc.build("status", mode="Normal")
                message.update(fields)
                before = len(world.sys.history.query(kind="adc_message"))
                self.assertEqual(adc.send_fields(message)["result"], "rejected")
                self.assertEqual(len(world.sys.history.query(kind="adc_message")), before + 1)
        self.assertEqual(world.sys.comm.pending_outbound(adc.id), 0)

    def test_NFR_SEC_03_nonfinite_poll_times_do_not_drain_the_outbox(self):
        world = World()
        adc = world.add_adc()
        world.sys.comm.send(adc.id, OutInventoryRequest())
        for ts in ("nan", "inf", "-inf"):
            signature = hmac.new(adc.key.encode(), f"outbox|{adc.id}|{ts}".encode(),
                                 hashlib.sha256).hexdigest()
            self.assertIn("error", json.loads(world.sys.poll_outbox(adc.id, ts, signature)))
            self.assertEqual(world.sys.comm.pending_outbound(adc.id), 1)

    def test_NFR_AVL_01_heartbeat_uses_the_scaled_policy_interval(self):
        policy = Policy(heartbeat_interval_s=80).scaled(20)
        with tempfile.TemporaryDirectory() as directory:
            world = World(os.path.join(directory, "history.db"), policy=policy)
            try:
                world.sys.tick()
                world.clock.advance(3)
                world.sys.tick()
                world.clock.advance(1)
                world.sys.tick()
                with open(os.path.join(directory, "uptime.log")) as heartbeat:
                    self.assertEqual(len(heartbeat.readlines()), 2)
            finally:
                world.sys.history._db.close()
