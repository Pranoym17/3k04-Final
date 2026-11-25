"""Alarm Manager module.

Responsibility: keep every alarm (raised by an ADC or by the Monitoring System
itself), run the alarm lifecycle Raised / Escalated / Acknowledged / Cleared,
and present the active alarms in the order staff must see them.
Secret: how alarms are stored, ordered and escalated. Routing alarms by ward
or adding a "snoozed" state changes this module only.

D1 traceability: FR-ALM-01 to FR-ALM-05, FR-CHM-02/-07/-08 (the alarm side),
FR-COM-07 (threshold alarm), A4, A5, A15, A16, hazard H2, D1 Figure 10.

State invariant I-ALM: an alarm key appears once; Escalated implies High;
only Cleared alarms have a clear time; the active list holds no Cleared alarm.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from .clock import Clock
from .domain import (AdcMessage, AlarmClearBody, AlarmNoticeBody, AlarmState, AlarmView,
                     Authorisation, Category, ChangeKind, FailSafe, Fn, MsgKind, Origin, Result,
                     Severity)
from .history_store import HistoryStore
from .policy import Policy
from .transition_table import (ILLEGAL, Ignore, Move, Refuse, Stay, TransitionTable)


class AlarmEvent(Enum):
    """Events accepted by the alarm lifecycle table."""
    ACK = "acknowledge (pharmacist or technician)"
    ACK_REFUSED = "acknowledge (other role)"
    NOTICE_SAME = "same alarm notice"
    ESCALATE_DUE = "after 2 min, high"
    CLEAR = "clearCondition"


S, E = AlarmState, AlarmEvent
# D1 Figure 10 table. Row = alarm state, column = event.
ALARM_TABLE = TransitionTable("alarm lifecycle", list(S), list(E), {
    S.RAISED: {E.ACK: Move(S.ACKNOWLEDGED, "ack"), E.ACK_REFUSED: Refuse("role may not acknowledge"),
               E.NOTICE_SAME: Ignore("duplicate notice"), E.ESCALATE_DUE: Move(S.ESCALATED, "escalate"),
               E.CLEAR: Move(S.CLEARED, "clear")},
    S.ESCALATED: {E.ACK: Move(S.ACKNOWLEDGED, "ack"), E.ACK_REFUSED: Refuse("role may not acknowledge"),
                  E.NOTICE_SAME: Ignore("duplicate notice"), E.ESCALATE_DUE: Stay(),
                  E.CLEAR: Move(S.CLEARED, "clear")},
    S.ACKNOWLEDGED: {E.ACK: Ignore("already acknowledged"), E.ACK_REFUSED: Refuse("role may not acknowledge"),
                     E.NOTICE_SAME: Ignore("duplicate notice"), E.ESCALATE_DUE: Stay(),
                     E.CLEAR: Move(S.CLEARED, "clear")},
    S.CLEARED: {E.ACK: Ignore("alarm already cleared"), E.ACK_REFUSED: Ignore("alarm already cleared"),
                E.NOTICE_SAME: Ignore("alarm already cleared"), E.ESCALATE_DUE: Stay(),
                E.CLEAR: Ignore("alarm already cleared")},
})


@dataclass
class _Alarm:
    key: str
    adc_id: str
    alarm_id: str
    alarm_type: str
    severity: Severity
    origin: Origin
    state: AlarmState
    adc_ts: Optional[float]
    receive_ts: float
    ack_by: Optional[str] = None
    ack_at: Optional[float] = None
    cleared_at: Optional[float] = None
    qualifier: str = ""


class AlarmManager:
    """Track ADC and monitoring alarms through their lifecycle."""
    def __init__(self, history: HistoryStore, clock: Clock, policy: Policy, mode):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._clock = clock
        self._p = policy
        self._mode = mode
        self._alarms: Dict[str, _Alarm] = {}
        self._seq = 0

    # ---------------------------------------------------- ADC-originated
    def on_alarm_notice(self, msg: AdcMessage) -> None:
        """FR-ALM-01, FR-ALM-03. Pre: msg.kind is ALARM_NOTICE and valid.
        Function table:
          N1 key unknown                      -> new alarm in state Raised, recorded
          N2 key known (any state)            -> event NOTICE_SAME: ignored and recorded"""
        if msg.kind is not MsgKind.ALARM_NOTICE:
            raise FailSafe("INV-ALM-KIND", str(msg.kind))
        b: AlarmNoticeBody = msg.body
        key = f"{msg.adc_id}/{b.alarm_id}"
        existing = self._alarms.get(key)
        if existing is not None:
            self._apply(existing, AlarmEvent.NOTICE_SAME, None)
            return
        a = _Alarm(key, msg.adc_id, b.alarm_id, b.alarm_type, b.severity, Origin.ADC,
                   AlarmState.RAISED, msg.adc_ts, msg.receive_ts)
        self._alarms[key] = a
        self._record(a, None, AlarmState.RAISED, "alarm notice received")
        self._check_invariant()

    def on_alarm_clear(self, msg: AdcMessage) -> None:
        """FR-ALM-03: only the ADC clears an ADC alarm (A5). A clear for an
        unknown alarm is ignored and recorded (FR-HIS-05)."""
        if msg.kind is not MsgKind.ALARM_CLEAR:
            raise FailSafe("INV-ALM-KIND", str(msg.kind))
        b: AlarmClearBody = msg.body
        a = self._alarms.get(f"{msg.adc_id}/{b.alarm_id}")
        if a is None or a.origin is not Origin.ADC:
            self._h.append(Category.SYSTEM, "ignored_event",
                           {"machine": "alarm", "event": "alarm clear", "alarm_id": b.alarm_id,
                            "reason": "no such ADC alarm"}, adc_id=msg.adc_id)
            return
        self._apply(a, AlarmEvent.CLEAR, "ADC reported cleared")

    # ------------------------------------------- Monitoring System alarms
    def raise_monitoring_alarm(self, adc_id: str, kind: str, severity: Severity,
                               qualifier: str = "") -> Optional[AlarmView]:
        """FR-CHM-02, FR-CHM-07, FR-COM-07. `kind` is "communication", "fault"
        or "threshold". Idempotent: when an alarm of the same adc, kind and
        qualifier is still active, nothing is raised and None is returned."""
        for a in self._alarms.values():
            if (a.origin is Origin.MONITORING and a.adc_id == adc_id and a.alarm_type == kind
                    and a.qualifier == qualifier and a.state is not AlarmState.CLEARED):
                return None
        while True:
            self._seq += 1
            alarm_id = f"MS-{self._seq:05d}"
            key = f"{adc_id}/{alarm_id}"
            if key not in self._alarms:
                break
        a = _Alarm(key, adc_id, alarm_id, kind, severity, Origin.MONITORING, AlarmState.RAISED,
                   None, self._clock.now(), qualifier=qualifier)
        self._alarms[key] = a
        self._record(a, None, AlarmState.RAISED, f"{kind} alarm raised")
        self._check_invariant()
        return self._view(a)

    def clear_monitoring_alarm(self, adc_id: str, kind: str, reason: str, qualifier: str = "") -> int:
        """A5: the Monitoring System clears its own alarms. Returns how many were cleared."""
        n = 0
        for a in list(self._alarms.values()):
            if (a.origin is Origin.MONITORING and a.adc_id == adc_id and a.alarm_type == kind
                    and a.qualifier == qualifier and a.state is not AlarmState.CLEARED):
                self._apply(a, AlarmEvent.CLEAR, reason)
                n += 1
        return n

    def clear_alarms_of(self, adc_id: str, reason: str) -> int:
        """D2 decision D10: when an ADC is deregistered nobody can clear its
        alarms any more, so every active alarm of that ADC is cleared."""
        n = 0
        for a in list(self._alarms.values()):
            if a.adc_id == adc_id and a.state is not AlarmState.CLEARED:
                self._apply(a, AlarmEvent.CLEAR, reason)
                n += 1
        return n

    # ---------------------------------------------------------- user actions
    def acknowledge(self, auth: Authorisation, key: str) -> Result:
        """FR-ALM-02. Pre: auth issued for Fn.ACK_ALARM. Post: alarm is
        Acknowledged with auth.user_id, or unchanged with a recorded reason.
        Function table:
          K1 system mode refuses acknowledgements (Fault)      -> refused
          K2 auth.fn is not ACK_ALARM  -> event ACK_REFUSED    -> refused, recorded
          K3 key unknown                                       -> refused
          K4 otherwise                 -> event ACK            -> table decides"""
        gate = self._mode.allows(ChangeKind.ACKNOWLEDGE)
        if not gate:
            return self._refuse(auth.user_id, gate.reason)
        a = self._alarms.get(key)
        if auth.fn is not Fn.ACK_ALARM:
            if a is not None:
                return self._apply(a, AlarmEvent.ACK_REFUSED, None, user=auth.user_id)
            return self._refuse(auth.user_id, "not permitted")
        if a is None:
            return self._refuse(auth.user_id, "no such alarm")
        return self._apply(a, AlarmEvent.ACK, None, user=auth.user_id)

    # ------------------------------------------------------------- timer
    def tick(self, now: float) -> None:
        """FR-ALM-04. A High alarm that is Raised and at least
        escalation_after_s old becomes Escalated."""
        for a in list(self._alarms.values()):
            if (a.state is AlarmState.RAISED and a.severity is Severity.HIGH
                    and now - a.receive_ts >= self._p.escalation_after_s):
                self._apply(a, AlarmEvent.ESCALATE_DUE, "unacknowledged for "
                            f"{int(self._p.escalation_after_s)} s")

    # ----------------------------------------------------------- queries
    def active_alarms(self) -> List[AlarmView]:
        """FR-ALM-05 ordering: Escalated first, then higher severity, then
        newer first. Cleared alarms are never in the list (FR-ALM-03)."""
        act = [a for a in self._alarms.values() if a.state is not AlarmState.CLEARED]
        act.sort(key=lambda a: (a.state is not AlarmState.ESCALATED, -a.severity.value, -a.receive_ts))
        return [self._view(a) for a in act]

    def has_escalated(self) -> bool:
        """Return whether an Escalated alarm remains active."""
        return any(a.state is AlarmState.ESCALATED for a in self._alarms.values())

    def active_count(self, adc_id: str) -> int:
        """Count the active alarms belonging to an ADC."""
        return sum(1 for a in self._alarms.values()
                   if a.adc_id == adc_id and a.state is not AlarmState.CLEARED)

    def alarms_between(self, start: float, end: float, adc_id: Optional[str] = None) -> List[AlarmView]:
        """Return alarms received in the inclusive time range."""
        out = [a for a in self._alarms.values()
               if start <= a.receive_ts <= end and (adc_id is None or a.adc_id == adc_id)]
        out.sort(key=lambda a: a.receive_ts)
        return [self._view(a) for a in out]

    def get(self, key: str) -> Optional[AlarmView]:
        """Return an immutable alarm view, or None for an unknown key."""
        a = self._alarms.get(key)
        return self._view(a) if a else None

    # ------------------------------------------------------------- restore
    def restore(self, events) -> None:
        """Rebuild every alarm and its state from `alarm_change` events."""
        for rec, _ in events:
            if rec.category is not Category.SYSTEM or rec.kind != "alarm_change":
                continue
            d = rec.data
            key = d["alarm_key"]
            if d["from"] is None:
                self._alarms[key] = _Alarm(
                    key, rec.adc_id, d["alarm_id"], d["alarm_type"], Severity[d["severity"]],
                    Origin(d["origin"]), AlarmState.RAISED, d.get("adc_ts"), rec.ts,
                    qualifier=d.get("qualifier", ""))
                if Origin(d["origin"]) is Origin.MONITORING:
                    self._seq = max(self._seq, int(d["alarm_id"][3:]))
            else:
                a = self._alarms[key]
                a.state = AlarmState(d["to"])
                if a.state is AlarmState.ACKNOWLEDGED:
                    a.ack_by, a.ack_at = rec.user_id, rec.ts
                if a.state is AlarmState.CLEARED:
                    a.cleared_at = rec.ts
        self._check_invariant()

    # ------------------------------------------------------------ internals
    def _apply(self, a: _Alarm, event: AlarmEvent, why: Optional[str], user: Optional[str] = None) -> Result:
        cell = ALARM_TABLE.lookup(a.state, event)
        if cell is ILLEGAL:
            raise FailSafe("FSM-ALARM-ILLEGAL", f"{a.state.name}/{event}")
        if isinstance(cell, Move):
            old = a.state
            a.state = cell.next
            now = self._clock.now()
            if cell.next is AlarmState.ACKNOWLEDGED:
                a.ack_by, a.ack_at = user, now
            if cell.next is AlarmState.CLEARED:
                a.cleared_at = now
            self._record(a, old, cell.next, why or event.value, user)
            self._check_invariant()
            return Result.success()
        if isinstance(cell, Ignore):
            self._h.append(Category.SYSTEM, "ignored_event",
                           {"machine": "alarm", "alarm_key": a.key, "state": a.state.value,
                            "event": event.value, "reason": cell.reason}, adc_id=a.adc_id, user_id=user)
            return Result.success(reason=cell.reason)
        if isinstance(cell, Refuse):
            self._h.append(Category.SYSTEM, "refused_event",
                           {"machine": "alarm", "alarm_key": a.key, "state": a.state.value,
                            "event": event.value, "reason": cell.reason}, adc_id=a.adc_id, user_id=user)
            return Result.refused(cell.reason)
        return Result.success()  # Stay: nothing to do, nothing to record

    def _record(self, a: _Alarm, old: Optional[AlarmState], new: AlarmState, why: str,
                user: Optional[str] = None) -> None:
        data = {"alarm_key": a.key, "alarm_id": a.alarm_id, "alarm_type": a.alarm_type,
                "severity": a.severity.name, "origin": a.origin.value, "adc_ts": a.adc_ts,
                "qualifier": a.qualifier, "from": old.value if old else None, "to": new.value,
                "reason": why}
        self._h.append(Category.SYSTEM, "alarm_change", data, adc_id=a.adc_id, user_id=user,
                       ts=self._clock.now() if old else a.receive_ts)
        if old is AlarmState.RAISED and new is AlarmState.ACKNOWLEDGED or \
                old is AlarmState.ESCALATED and new is AlarmState.ACKNOWLEDGED:
            self._h.append(Category.USER_ACTION, "alarm_acknowledged",
                           {"target": a.key, "old": old.value, "new": new.value}, adc_id=a.adc_id,
                           user_id=user)
        if new is AlarmState.ESCALATED:
            self._h.append(Category.SYSTEM, "escalation", {"alarm_key": a.key}, adc_id=a.adc_id)

    @staticmethod
    def _view(a: _Alarm) -> AlarmView:
        return AlarmView(a.key, a.adc_id, a.alarm_id, a.alarm_type, a.severity, a.origin, a.state,
                         a.adc_ts, a.receive_ts, a.ack_by, a.ack_at, a.cleared_at)

    def _check_invariant(self) -> None:
        for a in self._alarms.values():
            if a.state is AlarmState.ESCALATED and a.severity is not Severity.HIGH:
                raise FailSafe("INV-ALM-1", a.key)
            if (a.cleared_at is not None) != (a.state is AlarmState.CLEARED):
                raise FailSafe("INV-ALM-2", a.key)

    def _refuse(self, user_id: str, reason: str) -> Result:
        """Record a refused action without changing its result or application state."""
        self._h.append(Category.ACCESS, "action_refused",
                       {"module": "alarm_manager", "reason": reason}, user_id=user_id)
        return Result.refused(reason)
