"""History Store module.

Responsibility: keep every safety-relevant event, append only, and give it
back by ADC, user, category and time range.
Secret: the storage format and location (an SQLite file today). Callers see
`append`, `query`, `replay`, `test_write` and nothing about tables, files or
SQL. A move to a hospital database changes this file only.

Design points (D2 decisions D7 and D8):
  * There is no operation that edits or deletes an event (NFR-AUD-02). Two SQL
    triggers also reject UPDATE and DELETE, so even a bug cannot do it.
  * `append` never raises. When a write fails the event is kept in memory, in
    order, and the write listeners are told. When `test_write` succeeds the
    buffered events are written first, so no evidence is lost (H7).
  * Fields given as `secret` (ADC keys, password hashes) are stored but are
    never returned by `query`; only `replay`, used by the modules to rebuild
    their state after a restart, returns them.

Invariant I-HIS: sequence numbers strictly increase; stored events are never
changed; if `_pending` is not empty no later event is written before it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Callable, Dict, List, Optional, Tuple

from .clock import Clock
from .domain import Category, EventRecord
from .policy import Policy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL    NOT NULL,
    category TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    adc_id   TEXT,
    user_id  TEXT,
    data     TEXT    NOT NULL,
    secret   TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_adc  ON events(adc_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(category, kind);
CREATE INDEX IF NOT EXISTS ix_events_user ON events(user_id, ts);
CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, ts REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'history is append only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'history is append only'); END;
"""

Pending = Tuple[float, str, str, Optional[str], Optional[str], str, Optional[str]]


class HistoryStore:
    """Keep append-only events and secret replay data in SQLite."""
    def __init__(self, path: str, clock: Clock, policy: Policy):
        """Initialise the module with its injected collaborators and empty state."""
        self._clock = clock
        self._policy = policy
        self._lock = threading.RLock()
        self._pending: List[Pending] = []
        self._lost = 0
        self._inject_failure = False
        self._listeners: List[Callable[[bool], None]] = []
        self._notifying = False
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    # ------------------------------------------------------------ interface
    @property
    def retention_years(self) -> int:
        """NFR-REL-02. No operation of this module deletes an event, so
        retention is unlimited; the setting documents the minimum promised."""
        return self._policy.retention_years

    def add_write_listener(self, fn: Callable[[bool], None]) -> None:
        """Register fn(ok). Called with False on every failed write and with
        True when `test_write` succeeds. The listener is not a USES relation:
        the History Store does not know who listens."""
        self._listeners.append(fn)

    def append(self, category: Category, kind: str, data: Optional[dict] = None, *,
               adc_id: Optional[str] = None, user_id: Optional[str] = None,
               secret: Optional[dict] = None, ts: Optional[float] = None) -> Optional[int]:
        """Pre: category is a Category and `data` is JSON-serialisable.
        Post (store healthy): the event is stored and its sequence number is
        returned. Post (store failing): the event is kept in order in memory,
        listeners receive False, and None is returned. Never raises."""
        row: Pending = (
            self._clock.now() if ts is None else ts,
            category.value, kind, adc_id, user_id,
            json.dumps(data or {}, sort_keys=True, default=str),
            json.dumps(secret, sort_keys=True, default=str) if secret else None,
        )
        with self._lock:
            if self._pending:
                self._buffer(row)
                return None
            try:
                return self._write(row)
            except Exception:
                self._buffer(row)
            failed = True
        if failed:
            self._notify(False)
        return None

    def query(self, *, adc_id: Optional[str] = None, category: Optional[Category] = None,
              kind: Optional[str] = None, user_id: Optional[str] = None,
              start: Optional[float] = None, end: Optional[float] = None,
              limit: Optional[int] = None) -> List[EventRecord]:
        """Return stored events in sequence order. Secrets are never returned."""
        sql = "SELECT seq, ts, category, kind, adc_id, user_id, data FROM events WHERE 1=1"
        args: list = []
        if adc_id is not None:
            sql += " AND adc_id = ?"; args.append(adc_id)
        if category is not None:
            sql += " AND category = ?"; args.append(category.value)
        if kind is not None:
            sql += " AND kind = ?"; args.append(kind)
        if user_id is not None:
            sql += " AND user_id = ?"; args.append(user_id)
        if start is not None:
            sql += " AND ts >= ?"; args.append(start)
        if end is not None:
            sql += " AND ts <= ?"; args.append(end)
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"; args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._to_record(r) for r in rows]

    def replay(self) -> List[Tuple[EventRecord, Dict]]:
        """Every stored event with its secret part, oldest first. Used only by
        modules rebuilding their state at start-up; never shown to a user."""
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, ts, category, kind, adc_id, user_id, data, secret "
                "FROM events ORDER BY seq").fetchall()
        return [(self._to_record(r[:7]), json.loads(r[7]) if r[7] else {}) for r in rows]

    def test_write(self) -> bool:
        """Try one write. On success write any buffered events, tell the
        listeners True and return True. On failure return False."""
        with self._lock:
            try:
                self._probe()
                flushed = self._flush()
            except Exception:
                return False
        if flushed is not None and self._lost:
            lost, self._lost = self._lost, 0
            self.append(Category.SYSTEM, "events_lost", {"count": lost})
        self._notify(True)
        return True

    def pending_count(self) -> int:
        """Count events buffered after a storage write failure."""
        return len(self._pending)

    def failure_injected(self) -> bool:
        """True while the demonstration hook makes every write fail."""
        return self._inject_failure

    def inject_failure(self, on: bool) -> None:
        """Demonstration hook: make every write fail (on) or work again (off)."""
        self._inject_failure = bool(on)

    # ------------------------------------------------------------- internals
    def _write(self, row: Pending) -> int:
        if self._inject_failure:
            raise OSError("injected storage failure")
        cur = self._db.execute(
            "INSERT INTO events (ts, category, kind, adc_id, user_id, data, secret) "
            "VALUES (?,?,?,?,?,?,?)", row)
        return int(cur.lastrowid)

    def _probe(self) -> None:
        if self._inject_failure:
            raise OSError("injected storage failure")
        self._db.execute("INSERT OR REPLACE INTO probe (id, ts) VALUES (1, ?)",
                         (self._clock.now(),))

    def _buffer(self, row: Pending) -> None:
        if len(self._pending) >= self._policy.pending_write_limit:
            self._lost += 1
        else:
            self._pending.append(row)

    def _flush(self) -> Optional[int]:
        last = None
        while self._pending:
            last = self._write(self._pending[0])
            self._pending.pop(0)
        return last if last is not None else 0

    def _notify(self, ok: bool) -> None:
        if self._notifying:
            return
        self._notifying = True
        try:
            for fn in list(self._listeners):
                fn(ok)
        finally:
            self._notifying = False

    @staticmethod
    def _to_record(r) -> EventRecord:
        return EventRecord(seq=r[0], ts=r[1], category=Category(r[2]), kind=r[3],
                           adc_id=r[4], user_id=r[5], data=json.loads(r[6]))
