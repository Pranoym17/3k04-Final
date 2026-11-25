"""History Store: NFR-AUD-02, NFR-REL-01, NFR-REL-02, FR-HIS-02, FR-HIS-03, H7 (decision D8)."""
import os
import sqlite3
import tempfile
import unittest

from helpers import *                                   # noqa: F401,F403
from monitoring.clock import ManualClock
from monitoring.domain import Category
from monitoring.history_store import HistoryStore
from monitoring.policy import Policy


def store(path=":memory:", policy=None):
    c = ManualClock()
    return HistoryStore(path, c, policy or Policy()), c


class HistoryStoreTests(unittest.TestCase):
    def test_NFR_AUD_02_no_function_edits_or_deletes_an_event(self):
        h, _ = store()
        public = [n for n in dir(h) if not n.startswith("_")]
        for name in public:
            self.assertNotRegex(name, r"update|delete|remove|edit|modify|drop|clear")

    def test_NFR_AUD_02_sql_triggers_reject_update_and_delete(self):
        h, _ = store()
        h.append(Category.SYSTEM, "x", {"a": 1})
        with self.assertRaises(sqlite3.DatabaseError):
            h._db.execute("UPDATE events SET kind='y'")
        with self.assertRaises(sqlite3.DatabaseError):
            h._db.execute("DELETE FROM events")
        self.assertEqual(len(h.query()), 1)

    def test_FR_HIS_02_query_by_adc_and_time_range(self):
        h, c = store()
        for i in range(5):
            h.append(Category.MESSAGE, "adc_message", {"i": i}, adc_id="ADC-01", ts=100 + i)
        h.append(Category.MESSAGE, "adc_message", {"i": 9}, adc_id="ADC-02", ts=102)
        got = h.query(adc_id="ADC-01", start=101, end=103)
        self.assertEqual([e.data["i"] for e in got], [1, 2, 3])

    def test_NFR_REL_01_events_survive_a_restart(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "h.db")
        h, _ = store(p)
        h.append(Category.SYSTEM, "x", {"n": 1})
        h2, _ = store(p)
        self.assertEqual(h2.query()[0].data["n"], 1)

    def test_NFR_REL_02_retention_is_at_least_ten_years(self):
        h, _ = store()
        self.assertGreaterEqual(h.retention_years, 10)

    def test_secrets_are_stored_but_never_returned_by_query(self):
        h, _ = store()
        h.append(Category.USER_ACTION, "adc_registered", {"target": "A"}, secret={"key": "k1"})
        self.assertNotIn("k1", str(h.query()))
        self.assertEqual(h.replay()[0][1], {"key": "k1"})

    def test_H7_failed_write_is_buffered_in_order_and_flushed_on_test_write(self):
        h, _ = store()
        seen = []
        h.add_write_listener(seen.append)
        h.append(Category.SYSTEM, "a", {})
        h.inject_failure(True)
        self.assertIsNone(h.append(Category.SYSTEM, "b", {}))     # never raises
        h.inject_failure(False)
        h.append(Category.SYSTEM, "c", {})                         # must not overtake b
        self.assertEqual([e.kind for e in h.query()], ["a"])
        self.assertEqual(h.pending_count(), 2)
        self.assertTrue(h.test_write())
        self.assertEqual([e.kind for e in h.query()], ["a", "b", "c"])
        self.assertEqual(seen, [False, True])

    def test_H7_test_write_fails_while_storage_fails(self):
        h, _ = store()
        h.inject_failure(True)
        self.assertFalse(h.test_write())

    def test_H7_overflow_is_counted_and_recorded(self):
        h, _ = store(policy=Policy(pending_write_limit=2))
        h.inject_failure(True)
        for i in range(5):
            h.append(Category.SYSTEM, "e", {"i": i})
        h.inject_failure(False)
        h.test_write()
        kinds = [e.kind for e in h.query()]
        self.assertEqual(kinds.count("e"), 2)
        self.assertIn("events_lost", kinds)
        self.assertEqual([e for e in h.query(kind="events_lost")][0].data["count"], 3)


if __name__ == "__main__":
    unittest.main()
