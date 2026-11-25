"""Cabinet Health Monitor module.

Responsibility: decide, for every registered ADC, whether it is Online,
Reconnecting or Offline, from the messages that arrive and the time that
passes; raise the communication and fault alarms that follow; and answer
"is this ADC's data Stale?".
Secret: the connection-health policy: how silence is detected, which message
counts as proof of life, and what recovery needs. Changing "recovery needs a
fresh inventory report" to "recovery needs two status messages" changes the
table below and nothing else.

D1 traceability: FR-CHM-01 to FR-CHM-08, FR-COM-11, FR-MOD-01, NFR-SAF-02,
assumptions A1, A3, A9, A12, hazard H3, D1 Figure 9.

State invariants I-CHM:
  1. state == ONLINE or RECONNECTING  <=>  a deadline exists.
  2. state == ONLINE  =>  now < deadline = last valid receive + offline_timeout_s
     (`snapshot` expires overdue ADCs first, so no caller can see an overdue
     ADC as Online: NFR-SAF-02).
  3. stale(adc) == (state in {OFFLINE, RECONNECTING}); Stale is derived, never stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from .clock import Clock
from .domain import (AdcMessage, AdcMode, Category, ConnState, FailSafe, HealthSnapshot,
                     MsgKind, OutInventoryRequest, Outcome, Severity, StatusBody)
from .history_store import HistoryStore
from .policy import Policy
from .transition_table import (ILLEGAL, Ignore, Move, Refuse, Stay, TransitionTable)


class ConnEvent(Enum):
    """Events accepted by the cabinet connection table."""
    REGISTER = "registerADC"
    DEREGISTER = "deregisterADC"
    STATUS = "validStatus"
    INVENTORY = "validInventory"
    OTHER = "validOther"
    LATE = "lateMessage"
    INVALID = "invalidMessage"
    TIMEOUT = "timeout"
    RESTART = "systemRestart"


S, E = ConnState, ConnEvent
# D1 Figure 9 table plus the LATE column added in D2 (decision D3).
CONN_TABLE = TransitionTable("ADC connection", list(S), list(E), {
    S.UNREGISTERED: {
        E.REGISTER: Move(S.OFFLINE, "enter_offline"), E.DEREGISTER: Ignore("not registered"),
        E.STATUS: ILLEGAL, E.INVENTORY: ILLEGAL, E.OTHER: ILLEGAL, E.LATE: ILLEGAL,
        E.INVALID: Ignore("rejected and recorded by ADC Communication Manager"),
        E.TIMEOUT: Ignore("no timer"), E.RESTART: Stay()},
    S.OFFLINE: {
        E.REGISTER: Refuse("already registered"), E.DEREGISTER: Move(S.UNREGISTERED, "leave"),
        E.STATUS: Move(S.RECONNECTING, "start_reconnect"), E.INVENTORY: Stay("process_stale"),
        E.OTHER: Stay("process_stale"), E.LATE: Stay(), E.INVALID: Stay(), E.TIMEOUT: Ignore("already Offline"),
        E.RESTART: Stay("ensure_alarm")},
    S.RECONNECTING: {
        E.REGISTER: Refuse("already registered"), E.DEREGISTER: Move(S.UNREGISTERED, "leave"),
        E.STATUS: Stay(), E.INVENTORY: Move(S.ONLINE, "go_online"), E.OTHER: Stay(),
        E.LATE: Stay(), E.INVALID: Stay(), E.TIMEOUT: Move(S.OFFLINE, "fail_reconnect"),
        E.RESTART: Move(S.OFFLINE, "restart_offline")},
    S.ONLINE: {
        E.REGISTER: Refuse("already registered"), E.DEREGISTER: Move(S.UNREGISTERED, "leave"),
        E.STATUS: Stay("restart_timer"), E.INVENTORY: Stay("restart_timer"), E.OTHER: Stay("restart_timer"),
        E.LATE: Stay(), E.INVALID: Stay(), E.TIMEOUT: Move(S.OFFLINE, "go_offline"),
        E.RESTART: Move(S.OFFLINE, "restart_offline")},
})


@dataclass
class _Rec:
    state: ConnState = ConnState.UNREGISTERED
    deadline: Optional[float] = None
    last_valid: Optional[float] = None
    adc_mode: Optional[AdcMode] = None


class CabinetHealthMonitor:
    """Track receive-time connection deadlines and cabinet modes."""
    def __init__(self, history: HistoryStore, clock: Clock, policy: Policy, alarms, sender):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._clock = clock
        self._p = policy
        self._alarms = alarms      # Alarm Manager (raise / clear Monitoring System alarms)
        self._send = sender        # ADC Communication Manager (send OutInventoryRequest)
        self._recs: Dict[str, _Rec] = {}

    # ------------------------------------------------- registry notifications
    def on_registered(self, adc_id: str) -> None:
        """A3: a newly registered ADC counts as Offline until its first valid message."""
        rec = self._recs.setdefault(adc_id, _Rec())
        self._dispatch(adc_id, rec, ConnEvent.REGISTER, self._clock.now(), "registered")

    def on_deregistered(self, adc_id: str) -> None:
        """Remove the cabinet from connection tracking through its table event."""
        rec = self._recs.get(adc_id)
        if rec is None:
            return
        self._dispatch(adc_id, rec, ConnEvent.DEREGISTER, self._clock.now(), "deregistered")
        self._recs.pop(adc_id, None)

    # ------------------------------------------------------- message input
    def on_message(self, msg: AdcMessage) -> None:
        """FR-CHM-01, FR-CHM-04, FR-CHM-05, FR-CHM-07, FR-CHM-08.
        Pre: msg passed registration, authentication, format and duplicate checks.
        Post: the table row for (state, event) was applied. A Late message
        never restarts a timer and never moves the state (decision D3)."""
        rec = self._recs.get(msg.adc_id, _Rec())
        self._expire(msg.adc_id, rec)
        if msg.late:
            ev = ConnEvent.LATE
        elif msg.kind is MsgKind.STATUS:
            ev = ConnEvent.STATUS
        elif msg.kind is MsgKind.INVENTORY:
            ev = ConnEvent.INVENTORY
        else:
            ev = ConnEvent.OTHER
        self._dispatch(msg.adc_id, rec, ev, msg.receive_ts, "valid message", msg)
        if not msg.late:
            rec.last_valid = msg.receive_ts
        if msg.kind is MsgKind.STATUS and not msg.late:
            self._apply_mode(msg.adc_id, rec, msg.body)

    def on_invalid_message(self, adc_id: str) -> None:
        """A registered ADC sent a rejected message: the timer is kept (D1 table).
        The rejection itself was recorded by ADC Communication Manager."""
        rec = self._recs.get(adc_id)
        if rec is not None:
            self._expire(adc_id, rec)
            self._dispatch(adc_id, rec, ConnEvent.INVALID, self._clock.now(), "invalid message")

    # ------------------------------------------------------------- timers
    def tick(self, now: float) -> None:
        """Expire overdue cabinet connection deadlines."""
        for adc_id, rec in list(self._recs.items()):
            self._expire(adc_id, rec)

    def start(self) -> None:
        """FR-MOD-01: after a restart every registered ADC is Offline, with
        its communication alarm active, until it sends a valid message."""
        for adc_id, rec in list(self._recs.items()):
            self._dispatch(adc_id, rec, ConnEvent.RESTART, self._clock.now(), "system restart")

    # ----------------------------------------------------------- queries
    def state_of(self, adc_id: str) -> ConnState:
        """Return connection state after expiring any overdue deadline."""
        rec = self._recs.get(adc_id)
        if rec is None:
            return ConnState.UNREGISTERED
        self._expire(adc_id, rec)
        return rec.state

    def is_stale(self, adc_id: str) -> bool:
        """FR-CHM-03."""
        return self.state_of(adc_id) in (ConnState.OFFLINE, ConnState.RECONNECTING)

    def snapshot(self, adc_id: str) -> HealthSnapshot:
        """Return current connection facts with Stale derived from state."""
        st = self.state_of(adc_id)
        rec = self._recs.get(adc_id, _Rec())
        return HealthSnapshot(adc_id, st, st in (ConnState.OFFLINE, ConnState.RECONNECTING),
                              rec.last_valid, rec.adc_mode)

    # ------------------------------------------------------------- restore
    def restore(self, events) -> None:
        """Rebuild the registered ADCs and their last known state."""
        for rec_e, _ in events:
            if rec_e.category is Category.MESSAGE and rec_e.data.get("outcome") == Outcome.ACCEPTED.value:
                rec = self._recs.get(rec_e.adc_id)
                if rec is not None:
                    rec.last_valid = rec_e.data["receive_ts"]
                    if rec_e.data.get("msg_type") == MsgKind.STATUS.value:
                        rec.adc_mode = AdcMode(rec_e.data["content"]["mode"])
            if rec_e.category is not Category.SYSTEM:
                continue
            if rec_e.kind == "connection_change":
                adc = rec_e.adc_id
                to = ConnState(rec_e.data["to"])
                if to is ConnState.UNREGISTERED:
                    self._recs.pop(adc, None)
                else:
                    r = self._recs.setdefault(adc, _Rec())
                    r.state = to
                    r.deadline = None if to is ConnState.OFFLINE else float("inf")
                    # deadline is replaced by start(); it only has to exist (I-CHM 1)
        self._check_invariant()

    # ------------------------------------------------------------ internals
    def _expire(self, adc_id: str, rec: _Rec) -> None:
        if rec.deadline is not None and self._clock.now() >= rec.deadline \
                and rec.state in (ConnState.ONLINE, ConnState.RECONNECTING):
            self._dispatch(adc_id, rec, ConnEvent.TIMEOUT, self._clock.now(), "timeout")

    def _dispatch(self, adc_id: str, rec: _Rec, ev: ConnEvent, at: float, why: str,
                  msg: Optional[AdcMessage] = None) -> None:
        cell = CONN_TABLE.lookup(rec.state, ev)
        if cell is ILLEGAL:
            raise FailSafe("FSM-CONN-ILLEGAL", f"{adc_id} {rec.state.name}/{ev}")
        if isinstance(cell, Move):
            old = rec.state
            rec.state = cell.next
            self._run(cell.action, adc_id, rec, at, msg)
            self._h.append(Category.SYSTEM, "connection_change",
                           {"from": old.value, "to": rec.state.value, "reason": f"{why}: {cell.action}"},
                           adc_id=adc_id, ts=at)
        elif isinstance(cell, Stay):
            self._run(cell.action, adc_id, rec, at, msg)
        elif isinstance(cell, Ignore):
            self._h.append(Category.SYSTEM, "ignored_event",
                           {"machine": "connection", "state": rec.state.value, "event": ev.value,
                            "reason": cell.reason}, adc_id=adc_id)
        elif isinstance(cell, Refuse):
            self._h.append(Category.SYSTEM, "refused_event",
                           {"machine": "connection", "state": rec.state.value, "event": ev.value,
                            "reason": cell.reason}, adc_id=adc_id)
        self._check_invariant()

    def _run(self, action: str, adc_id: str, rec: _Rec, at: float, msg: Optional[AdcMessage]) -> None:
        p = self._p
        if action == "enter_offline":
            rec.deadline = None
            self._alarms.raise_monitoring_alarm(adc_id, "communication", Severity.HIGH)
        elif action == "start_reconnect":
            rec.deadline = at + p.reconnect_timeout_s
            rec.last_valid = at
            self._send.send(adc_id, OutInventoryRequest())          # FR-COM-11
        elif action == "go_online":
            rec.deadline = at + p.offline_timeout_s
            rec.last_valid = at
            self._alarms.clear_monitoring_alarm(adc_id, "communication", "ADC recovered")
        elif action == "restart_timer":
            rec.deadline = at + p.offline_timeout_s
            rec.last_valid = at
        elif action in ("go_offline", "restart_offline"):
            rec.deadline = None
            self._alarms.raise_monitoring_alarm(adc_id, "communication", Severity.HIGH)   # FR-CHM-02
        elif action == "fail_reconnect":
            rec.deadline = None                                       # alarm stays active
        elif action == "ensure_alarm":
            self._alarms.raise_monitoring_alarm(adc_id, "communication", Severity.HIGH)
        elif action == "leave":
            rec.deadline = None
        elif action in ("", "process_stale"):
            pass
        else:  # a table cell names an action this module does not implement
            raise FailSafe("TBL-CONN-ACTION", action)

    def _apply_mode(self, adc_id: str, rec: _Rec, body: StatusBody) -> None:
        """FR-CHM-07 / FR-CHM-08."""
        rec.adc_mode = body.mode
        if body.mode is AdcMode.FAULT:
            self._alarms.raise_monitoring_alarm(adc_id, "fault", Severity.HIGH)
        else:
            self._alarms.clear_monitoring_alarm(adc_id, "fault", f"ADC reported {body.mode.value}")

    def _check_invariant(self) -> None:
        for adc_id, rec in self._recs.items():
            has_deadline = rec.deadline is not None
            if has_deadline != (rec.state in (ConnState.ONLINE, ConnState.RECONNECTING)):
                raise FailSafe("INV-CHM-1", f"{adc_id} {rec.state.name}")
