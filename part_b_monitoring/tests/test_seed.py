"""Demo credentials, migration and replay without weakening normal accounts."""
import unittest

from helpers import World
from monitoring.domain import Fn, Role
from monitoring.seed import ACCOUNTS, seed
from monitoring.system import MonitoringSystem


class DemoSeedTests(unittest.TestCase):
    def test_demo_accounts_use_shared_password_and_seeding_is_idempotent(self):
        system = MonitoringSystem()
        system.start()
        seed(system)
        for uid, role, password in ACCOUNTS:
            result = system.access.login(uid, "12345")
            self.assertTrue(result.ok, uid)
            self.assertEqual(system.access.session(result.value).role, role)
        count = len(system.history.replay())
        self.assertEqual(seed(system), [])
        self.assertEqual(len(system.history.replay()), count)

    def test_existing_demo_password_changes_survive_replay(self):
        world = World()
        world.add_adc()
        self.assertIn("demo passwords", seed(world.sys))
        restored = MonitoringSystem(clock=world.clock)
        restored.start()
        restored.access.restore(world.sys.history.replay())
        for uid, role, password in ACCOUNTS:
            result = restored.access.login(uid, "12345")
            self.assertTrue(result.ok, uid)
            self.assertEqual(restored.access.session(result.value).role, role)
        self.assertFalse(restored.access.login("admin1", "admin-pass-1").ok)
        self.assertNotIn("12345", str(world.sys.history.query()))
        self.assertEqual(world.sys.config.registered_ids(), ["ADC-01"])

    def test_demo_seed_preserves_disabled_state_and_ends_old_sessions(self):
        world = World()
        old_token = world.tokens["pharm1"]
        admin = world.auth("admin1", Fn.ADMIN)
        world.sys.access.disable_user(admin, "nurse1")
        seed(world.sys, with_adcs=False)
        self.assertIsNone(world.sys.access.session(old_token))
        self.assertFalse(world.sys.access.login("nurse1", "12345").ok)

    def test_ordinary_account_creation_still_refuses_short_passwords(self):
        world = World()
        result = world.sys.access.create_user(world.auth("admin1", Fn.ADMIN),
                                              "nurse3", Role.NURSE, "12345")
        self.assertFalse(result.ok)
        self.assertIn("8 characters", result.reason)
