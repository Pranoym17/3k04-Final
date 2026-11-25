"""Application Composition module (composition root).

Responsibility: create every module once, connect them (the USES relation and
the callbacks), rebuild state from the History Store at start-up, and drive
the timers.
Secret: how the modules are wired, in which order messages reach them, and the
threading model (one lock; every inbound operation runs under it, so ADC
messages and user actions are processed one at a time: NFR-PRF-03 asks that no
message is lost, not that they run in parallel).

This is the only file that imports all other modules.

USES hierarchy (an arrow reads "uses"; no cycle exists):
  level 0  domain, clock, policy, transition_table
  level 1  history_store
  level 2  mode_manager
  level 3  access_control, inventory, alarm_manager, adc_comm
  level 4  health_monitor            (alarms, comm)
  level 5  config_manager            (mode, comm, health, inventory, alarms)
           request_handler           (mode, comm, inventory)
  level 6  report_generator          (inventory, health, alarms, config)
  level 7  system                    (this file: creates and wires levels 0 to 6)
  level 8  web (dashboard, adc_gateway)  (use the objects of this file; run_monitoring.py
                                     mounts both on one Flask application)
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from .access_control import AccessControl
from .adc_comm import AdcCommunicationManager
from .alarm_manager import AlarmManager
from .clock import Clock, SystemClock
from .config_manager import ConfigurationManager
from .domain import Category, MsgKind, Result
from .health_monitor import CabinetHealthMonitor
from .history_store import HistoryStore
from .inventory import InventorySupervisor
from .mode_manager import SystemModeManager
from .policy import Policy
from .report_generator import ReportGenerator
from .request_handler import RequestHandler
from .seed import ACCOUNTS


class MonitoringSystem:
    """Construct, connect, restore and serialise the monitoring modules."""
    def __init__(self, db_path: str = ":memory:", clock: Optional[Clock] = None,
                 policy: Optional[Policy] = None, demo: bool = False):
        """Initialise the module with its injected collaborators and empty state."""
        self.clock = clock or SystemClock()
        self.policy = policy or Policy()
        self.demo = demo
        self.lock = threading.RLock()
        self._db_path = db_path
        self._started = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next_beat = 0.0

        c, p = self.clock, self.policy
        self.history = HistoryStore(db_path, c, p)
        self.mode = SystemModeManager(self.history, c, p)
        self.access = AccessControl(self.history, c, p, self.mode)
        self.comm = AdcCommunicationManager(self.history, c, p)
        self.alarms = AlarmManager(self.history, c, p, self.mode)
        self.inventory = InventorySupervisor(self.history, p)
        self.health = CabinetHealthMonitor(self.history, c, p, self.alarms, self.comm)
        self.config = ConfigurationManager(self.history, c, p, self.mode, self.comm, self.health,
                                           self.inventory, self.alarms)
        self.requests = RequestHandler(self.history, c, p, self.mode, self.comm, self.inventory)
        self.reports = ReportGenerator(self.history, c, self.inventory, self.health, self.alarms, self.config)
        self._wire()

    # ------------------------------------------------------------------ wiring
    def _wire(self) -> None:
        comm = self.comm
        # Health sees every valid message first, so an overdue ADC is Offline
        # before the message is processed (timeout precedes arrival).
        for kind in MsgKind:
            comm.register_handler(kind, self.health.on_message)
        comm.register_handler(MsgKind.INVENTORY, self.inventory.on_inventory_report)
        comm.register_handler(MsgKind.ALARM_NOTICE, self.alarms.on_alarm_notice)
        comm.register_handler(MsgKind.ALARM_CLEAR, self.alarms.on_alarm_clear)
        comm.register_handler(MsgKind.REQUEST, self.requests.on_request)
        comm.register_handler(MsgKind.CONFIG_ACK, self.config.on_config_ack)
        comm.add_invalid_listener(self.health.on_invalid_message)
        self.config.add_registry_listener(self._on_registry)

    def _on_registry(self, event: str, adc_id: str, key: Optional[str]) -> None:
        self.comm.on_registry_change(event, adc_id, key)
        if event == "registered":
            self.health.on_registered(adc_id)
        elif event == "deregistered":
            self.health.on_deregistered(adc_id)
            self.inventory.forget(adc_id)
            self.alarms.clear_alarms_of(adc_id, "ADC deregistered")

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Rebuild state from the history, test the store, set every ADC Offline."""
        with self.lock:
            events = self.history.replay()
            self.access.restore(events)
            self.comm.restore(events)
            self.inventory.restore(events)
            self.alarms.restore(events)
            self.health.restore(events)
            self.config.restore(events)
            self.requests.restore(events)
            self.mode.start()
            self.health.start()
            self.history.append(Category.SYSTEM, "system_start",
                                {"registered_adcs": len(self.config.registered_ids()),
                                 "mode": self.mode.mode().value})
            self._started = True

    def tick(self) -> None:
        """Run all module deadlines and the heartbeat under the system lock."""
        with self.lock:
            now = self.clock.now()
            self.health.tick(now)
            self.alarms.tick(now)
            self.config.tick(now)
            self.requests.tick(now)
            self.mode.tick(now)
            self._heartbeat(now)

    def run_ticker(self) -> None:
        """Start the background thread that calls tick() every tick_interval_s."""
        def loop():
            while not self._stop.wait(self.policy.tick_interval_s):
                try:
                    self.tick()
                except Exception as e:  # a defect must be visible but must not kill the timers
                    self.history.append(Category.SYSTEM, "tick_error", {"error": repr(e)})
        self._thread = threading.Thread(target=loop, name="monitoring-ticker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the background ticker to stop."""
        self._stop.set()

    # ------------------------------------------------------------ ADC interface
    def handle_adc_bytes(self, raw: bytes) -> bytes:
        """Process an inbound ADC message under the single system lock."""
        with self.lock:
            return self.comm.receive(raw)

    def poll_outbox(self, adc_id: str, ts: str, auth: str) -> bytes:
        """Process an authenticated outbox poll under the single system lock."""
        with self.lock:
            return self.comm.fetch_outbox(adc_id, ts, auth)

    # ---------------------------------------------------------- demo entry
    def _demo_accounts(self) -> list:
        """Available seeded accounts for the demonstration picker."""
        if not self.demo:
            return []
        known = {(uid, role) for uid, role, password in ACCOUNTS}
        return [u for u in self.access.list_users()
                if (u["user_id"], u["role"]) in known and not u["disabled"]]

    def _demo_login(self, user_id: str) -> Result:
        """Use ordinary sessions and permissions; only demo mode supplies credentials."""
        available = {u["user_id"] for u in self._demo_accounts()}
        for uid, role, password in ACCOUNTS:
            if uid == user_id and uid in available:
                return self.access.login(uid, password)
        return self.access.login(user_id, "")

    # --------------------------------------------------------------- uptime log
    def _heartbeat(self, now: float) -> None:
        """NFR-AVL-01: log each policy heartbeat (one minute by default)."""
        if self._db_path == ":memory:" or now < self._next_beat:
            return
        self._next_beat = now + self.policy.heartbeat_interval_s
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(self._db_path)), "uptime.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{now:.0f} alive mode={self.mode.mode().value}\n")
        except OSError:
            pass
