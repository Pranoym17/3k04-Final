"""System Mode Manager module.

Responsibility: own the operating mode of the Monitoring System (Starting,
Normal, Maintenance, Fault), decide which user data changes the current mode
allows, and bring the system out of Fault when storage works again.
Secret: the mode rules (which mode refuses which change) and the way a storage
failure is detected. A new mode or a different refusal rule changes this file
and the table below only.

D1 traceability: FR-MOD-02 to FR-MOD-06 (FR-MOD-01 is realised by Cabinet
Health Monitor), hazard H7, D1 Figure 11.

State invariant I-MOD: mode == FAULT implies a banner text exists; the mode
is Normal only after a successful test write since the last failed write.
"""
from __future__ import annotations

from enum import Enum
from typing import Callable, List, Optional

from .clock import Clock
from .domain import Authorisation, Category, ChangeKind, FailSafe, Fn, Result, SysMode
from .history_store import HistoryStore
from .policy import Policy
from .transition_table import (ILLEGAL, Ignore, Move, Refuse, Stay, TransitionTable)


class ModeEvent(Enum):
    """Events accepted by the system mode table."""
    TEST_OK = "testWriteSucceeded"
    WRITE_FAILED = "writeFailed"
    SWITCH_MAINT = "switchToMaintenance"
    SWITCH_NORMAL = "switchToNormal"
    RESTART = "systemRestart"


S, E = SysMode, ModeEvent
# D1 Figure 11 table. Row = current mode, column = event.
MODE_TABLE = TransitionTable("system mode", list(S), list(E), {
    S.STARTING: {E.TEST_OK: Move(S.NORMAL, "banner_off"), E.WRITE_FAILED: Move(S.FAULT, "banner_fault"),
                 E.SWITCH_MAINT: Refuse("system is starting"), E.SWITCH_NORMAL: Refuse("system is starting"),
                 E.RESTART: Stay()},
    S.NORMAL: {E.TEST_OK: Stay(), E.WRITE_FAILED: Move(S.FAULT, "banner_fault"),
               E.SWITCH_MAINT: Move(S.MAINTENANCE, "banner_maintenance"),
               E.SWITCH_NORMAL: Ignore("already in Normal mode"), E.RESTART: Move(S.STARTING, "banner_off")},
    S.MAINTENANCE: {E.TEST_OK: Stay(), E.WRITE_FAILED: Move(S.FAULT, "banner_fault"),
                    E.SWITCH_MAINT: Ignore("already in Maintenance mode"),
                    E.SWITCH_NORMAL: Move(S.NORMAL, "banner_off"), E.RESTART: Move(S.STARTING, "banner_off")},
    S.FAULT: {E.TEST_OK: Move(S.NORMAL, "banner_off"), E.WRITE_FAILED: Stay(),
              E.SWITCH_MAINT: Refuse("system is in Fault mode"), E.SWITCH_NORMAL: Refuse("system is in Fault mode"),
              E.RESTART: Move(S.STARTING, "banner_off")},
})

# Function table for allows(): rows = ChangeKind, columns = mode. True = allowed.
K = ChangeKind
ALLOWED = {
    K.THRESHOLD:    {S.STARTING: False, S.NORMAL: True, S.MAINTENANCE: False, S.FAULT: False},
    K.REGISTRATION: {S.STARTING: False, S.NORMAL: True, S.MAINTENANCE: False, S.FAULT: False},
    K.ACCOUNT:      {S.STARTING: False, S.NORMAL: True, S.MAINTENANCE: True,  S.FAULT: False},
    K.ACKNOWLEDGE:  {S.STARTING: False, S.NORMAL: True, S.MAINTENANCE: True,  S.FAULT: False},
    K.DECISION:     {S.STARTING: False, S.NORMAL: True, S.MAINTENANCE: True,  S.FAULT: False},
}

BANNERS = {
    "banner_fault": "FAULT: events cannot be stored. Data changes are refused. ADC alarms are still shown.",
    "banner_maintenance": "MAINTENANCE: threshold and ADC registration changes are refused.",
    "banner_off": None,
}


class SystemModeManager:
    """Own system mode, write recovery and permitted change kinds."""
    def __init__(self, history: HistoryStore, clock: Clock, policy: Policy):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._clock = clock
        self._p = policy
        self._mode = SysMode.STARTING
        self._banner: Optional[str] = None
        self._next_test = 0.0
        self._listeners: List[Callable[[SysMode, SysMode], None]] = []
        history.add_write_listener(self._on_write_result)

    # ----------------------------------------------------------- interface
    def mode(self) -> SysMode:
        """Return the current system mode."""
        return self._mode

    def banner(self) -> Optional[str]:
        """FR-MOD-03: text shown on every session while a mode needs one."""
        return self._banner

    def add_mode_listener(self, fn: Callable[[SysMode, SysMode], None]) -> None:
        """Register a callback for system mode changes."""
        self._listeners.append(fn)

    def start(self) -> SysMode:
        """Make the start-up test write. Post: mode is Normal when it succeeds,
        Fault when it fails."""
        ok = self._h.test_write()
        self._dispatch(ModeEvent.TEST_OK if ok else ModeEvent.WRITE_FAILED, "start-up test write")
        self._next_test = self._clock.now() + self._p.fault_test_interval_s
        return self._mode

    def allows(self, kind: ChangeKind) -> Result:
        """FR-MOD-03, FR-MOD-06. Total function over ChangeKind x SysMode (table ALLOWED)."""
        if kind not in ALLOWED:
            raise FailSafe("TBL-MODE-CHANGE", str(kind))
        if ALLOWED[kind][self._mode]:
            return Result.success()
        return Result.refused(f"{kind.value} refused: system is in {self._mode.value} mode")

    def switch_to_maintenance(self, auth: Authorisation) -> Result:
        """Request an authorised switch to Maintenance through the mode table."""
        return self._switch(auth, ModeEvent.SWITCH_MAINT)

    def switch_to_normal(self, auth: Authorisation) -> Result:
        """Request an authorised switch to Normal through the mode table."""
        return self._switch(auth, ModeEvent.SWITCH_NORMAL)

    def tick(self, now: float) -> None:
        """FR-MOD-04. In Fault mode, test the store every fault_test_interval_s."""
        if self._mode is SysMode.FAULT and now >= self._next_test:
            self._next_test = now + self._p.fault_test_interval_s
            ok = self._h.test_write()
            if ok:
                self._dispatch(ModeEvent.TEST_OK, "test write succeeded")

    def restart(self) -> None:
        """System restart event (used by tests; a real restart builds a new object)."""
        self._dispatch(ModeEvent.RESTART, "restart")

    # ------------------------------------------------------------ internals
    def _switch(self, auth: Authorisation, event: ModeEvent) -> Result:
        if auth.fn is not Fn.ADMIN:
            return self._refuse(auth.user_id, "not permitted")
        before = self._mode
        res = self._dispatch(event, f"switch by {auth.user_id}", user=auth.user_id)
        if res.ok and self._mode is not before:
            self._h.append(Category.USER_ACTION, "mode_switch",
                           {"old": before.value, "new": self._mode.value}, user_id=auth.user_id)
        return res

    def _on_write_result(self, ok: bool) -> None:
        if not ok:
            self._dispatch(ModeEvent.WRITE_FAILED, "write to History Store failed")

    def _dispatch(self, event: ModeEvent, why: str, user: Optional[str] = None) -> Result:
        cell = MODE_TABLE.lookup(self._mode, event)
        if cell is ILLEGAL:
            raise FailSafe("FSM-MODE-ILLEGAL", f"{self._mode.name}/{event}")
        if isinstance(cell, Move):
            if cell.action not in BANNERS:
                raise FailSafe("TBL-MODE-ACTION", cell.action)
            old = self._mode
            self._mode = cell.next
            self._banner = BANNERS.get(cell.action)
            self._h.append(Category.SYSTEM, "mode_change",
                           {"from": old.value, "to": self._mode.value, "reason": why}, user_id=user)
            for fn in list(self._listeners):
                fn(old, self._mode)
            self._check_invariant()
            return Result.success()
        if isinstance(cell, Ignore):
            self._h.append(Category.SYSTEM, "ignored_event",
                           {"machine": "mode", "state": self._mode.value, "event": event.value,
                            "reason": cell.reason}, user_id=user)
            return Result.success(reason=cell.reason)
        if isinstance(cell, Refuse):
            self._h.append(Category.SYSTEM, "refused_event",
                           {"machine": "mode", "state": self._mode.value, "event": event.value,
                            "reason": cell.reason}, user_id=user)
            return Result.refused(cell.reason)
        return Result.success()  # Stay

    def _check_invariant(self) -> None:
        if self._mode is SysMode.FAULT and not self._banner:
            raise FailSafe("INV-MOD-1", "Fault mode without banner")

    def _refuse(self, user_id: str, reason: str) -> Result:
        """Record a refused action without changing its result or application state."""
        self._h.append(Category.ACCESS, "action_refused",
                       {"module": "mode_manager", "reason": reason}, user_id=user_id)
        return Result.refused(reason)
