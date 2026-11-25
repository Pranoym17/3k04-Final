"""Deliverable 3 tests for the ADC safety requirements that had no unit test.

SR-ADC-03 lock feedback fault, SR-ADC-04 session expiry and inactive accounts,
SR-ADC-05 ordered outbound queue and link loss. Added for Deliverable 3; the
Deliverable 2 tests are unchanged.
"""
import unittest

from adc.behaviour.link_state import LinkState, LinkStateMachine
from adc.behaviour.mode_manager import Mode
from adc.behaviour.timing_policy import TimingPolicy
from adc.decisions.outbound_queue import OutboundQueue
from adc.hardware.clock import SimulatedClock
from tests.test_invariants import build


# Builds the same two drawer cabinet as the invariant tests, plus an inactive user.
def build_with_inactive():
    from adc.app import AdcApplication
    from adc.behaviour.authorization import Role
    from adc.hardware.credential_reader import SimulatedCredentialReader
    from adc.hardware.drawer_sensor import SimulatedDrawerSensor
    from adc.hardware.lock_driver import SimulatedLockDriver
    from adc.hardware.removal_sensor import SimulatedRemovalSensor
    clock = SimulatedClock()
    app = AdcApplication("ADC-T", clock, SimulatedLockDriver(["D1"]), SimulatedDrawerSensor(["D1"]),
                         SimulatedCredentialReader({"card-old": "u-old"}), SimulatedRemovalSensor(),
                         ["D1"], [("MORPH-10MG", "D1", 12, 40)], {"u-old": Role.NURSE},
                         inactive_users={"u-old"})
    app.start_up()
    return app


class LockFault(unittest.TestCase):
    # SR-ADC-03 boundary. A mismatch shorter than 2 s is tolerated; at 2 s the
    # drawer is inhibited, every drawer is locked and the cabinet enters FAULT.
    def test_SR_ADC_03_lock_mismatch_enters_fault_at_two_seconds(self):
        app, clock, lock, drawers, removal = build()
        lock.inject_feedback_fault("D2")
        app.poll_hardware()
        clock.advance(1.9)
        self.assertIn("within tolerance", app.poll_hardware())
        self.assertIn("NORMAL", app.banner())
        clock.advance(0.1)
        self.assertIn("LOCK_MISMATCH on D2", app.poll_hardware())
        self.assertIn("FAULT", app.banner())
        report = {d: (state, lk) for d, state, lk in app.drawer_report()}
        self.assertEqual(report["D2"], ("INHIBITED", "locked"))
        self.assertEqual(report["D1"][1], "locked")
        # and a dispense is now refused
        app.authenticate("card-nurse")
        tx, msg = app.request_dispense("MORPH-10MG", 1)
        self.assertIsNone(tx)
        self.assertIn("reason    : mode blocks", msg)


class Sessions(unittest.TestCase):
    # SR-ADC-04 boundary. 119 s idle keeps the session; 120 s ends it.
    def test_SR_ADC_04_session_expires_at_120_seconds_idle(self):
        app, clock, lock, drawers, removal = build()
        app.authenticate("card-nurse")
        clock.advance(119)
        tx, _ = app.request_dispense("MORPH-10MG", 1)
        self.assertIsNotNone(tx)                     # request_dispense touched the session
        clock.advance(120)
        tx2, msg = app.request_dispense("MORPH-10MG", 1)
        self.assertIsNone(tx2)
        self.assertIn("reason    : session expired", msg)

    # SR-ADC-04 off normal. An inactive account never opens a session.
    def test_SR_ADC_04_inactive_account_is_refused(self):
        app = build_with_inactive()
        user, msg = app.authenticate("card-old")
        self.assertIsNone(user)
        tx, msg = app.request_dispense("MORPH-10MG", 1)
        self.assertIsNone(tx)


class OutboundOrder(unittest.TestCase):
    # SR-ADC-05. A message leaves the queue only on an acknowledgement carrying
    # its own id; a wrong id clears nothing and order is kept.
    def test_SR_ADC_05_queue_clears_only_on_matching_ack_in_order(self):
        q = OutboundQueue()
        q.enqueue("MSG-00001", "DISPENSE_RESULT", {"tx": "TX-0001"})
        q.enqueue("MSG-00002", "INVENTORY_UPDATE", {"item": "MORPH-10MG"})
        self.assertFalse(q.acknowledge("MSG-99999"))
        self.assertEqual([m for _, m, _ in q.describe_pending()], ["MSG-00001", "MSG-00002"])
        self.assertTrue(q.acknowledge("MSG-00002"))
        self.assertFalse(q.acknowledge("MSG-00002"))          # a repeated ack changes nothing
        self.assertEqual(q.next_to_send().msg_id, "MSG-00001")

    # SR-ADC-05 off normal. Losing the link keeps local dispensing working and
    # keeps every message; the link never changes the cabinet mode.
    def test_SR_ADC_05_link_loss_keeps_messages_and_never_changes_mode(self):
        app, clock, lock, drawers, removal = build()
        clock.advance(16)
        app.poll_hardware()
        self.assertIn("LINK: OFFLINE", app.banner())
        self.assertIn("MODE: NORMAL", app.banner())
        app.authenticate("card-nurse")
        app.request_dispense("MORPH-10MG", 1)
        app.confirm_dispense()
        removal.set_confirmed("D1", True)
        drawers.set_closed("D1")
        self.assertIn("COMPLETED", app.close_drawer_and_complete())
        kinds = [k for _, _, k in app.pending_messages()]
        self.assertEqual(kinds[-2:], ["DISPENSE_RESULT", "INVENTORY_UPDATE"])

    # SR-ADC-05 boundary. Link declared down at exactly 3 missed 5 s heartbeats.
    def test_SR_ADC_05_link_declared_down_at_fifteen_seconds(self):
        clock = SimulatedClock()
        link = LinkStateMachine(clock, TimingPolicy())
        clock.advance(14.9)
        self.assertEqual(link.tick(), (False, "ignored"))
        clock.advance(0.1)
        self.assertEqual(link.tick(), (True, "enter_offline"))
        self.assertIs(link.state(), LinkState.OFFLINE)


if __name__ == "__main__":
    unittest.main()
