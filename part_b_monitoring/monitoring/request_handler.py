"""Request Handler module.

Responsibility: answer the requests an ADC sends: refill requests (decided
from the slot's Low flag) and maintenance requests (decided by the first
technician, or rejected when nobody decides in time).
Secret: the decision rules for each request type. A new request type (for
example "calibration") is one more branch here and one more message type in
ADC Communication Manager.

D1 traceability: FR-REQ-01 to FR-REQ-05, NFR-PRF-02, A17, hazards H4, H5.

State invariant I-REQ: every request gets at most one reply; a maintenance
request is pending until exactly one reply is sent for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .clock import Clock
from .domain import (AdcMessage, Authorisation, Category, ChangeKind, FailSafe, Fn, MsgKind,
                     OutRequestReply, Outcome, RequestBody, Result)
from .history_store import HistoryStore
from .policy import Policy


@dataclass
class MaintenanceRequest:
    """A maintenance request awaiting its first decision or timeout."""
    key: str                 # "<adc_id>/<request_id>"
    adc_id: str
    request_id: str
    received_at: float


class RequestHandler:
    """Decide refill requests and manage pending maintenance decisions."""
    def __init__(self, history: HistoryStore, clock: Clock, policy: Policy, mode, sender, inventory):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._clock = clock
        self._p = policy
        self._mode = mode
        self._send = sender
        self._inv = inventory
        self._pending: Dict[str, MaintenanceRequest] = {}
        self._answered: set = set()

    # ---------------------------------------------------------- inbound
    def on_request(self, msg: AdcMessage) -> None:
        """FR-REQ-01. Pre: msg.kind is REQUEST and valid.
        Function table for a REFILL request, evaluated per slot named in it:
          Q1 request is Late                        -> Reject "request too old, send it again"
          Q2 slot not known for that ADC            -> Reject "unknown slot"          (FR-REQ-02)
          Q3 slot is flagged Low                    -> Accept "slot is Low"           (FR-REQ-03)
          Q4 slot is not flagged Low                -> Reject "not Low"               (FR-REQ-03)
        A MAINTENANCE request is Late -> Reject, otherwise it becomes pending (FR-REQ-04)."""
        if msg.kind is not MsgKind.REQUEST:
            raise FailSafe("INV-REQ-KIND", str(msg.kind))
        b: RequestBody = msg.body
        key = f"{msg.adc_id}/{b.request_id}"
        if key in self._answered or key in self._pending:
            self._h.append(Category.SYSTEM, "ignored_event",
                           {"machine": "request", "event": "request", "request_id": b.request_id,
                            "reason": "request ID already handled"}, adc_id=msg.adc_id)
            return
        if b.request_type == "refill":
            self._answered.add(key)
            for slot in b.slots:
                if msg.late:
                    self._reply(msg.adc_id, b.request_id, "reject", "request too old, send it again", slot)
                elif not self._inv.has_slot(msg.adc_id, slot):
                    self._reply(msg.adc_id, b.request_id, "reject", "unknown slot", slot)
                elif self._inv.is_low(msg.adc_id, slot):
                    self._reply(msg.adc_id, b.request_id, "accept", "slot is Low", slot)
                else:
                    self._reply(msg.adc_id, b.request_id, "reject", "not Low", slot)
        elif b.request_type == "maintenance":
            if msg.late:
                self._answered.add(key)
                self._reply(msg.adc_id, b.request_id, "reject", "request too old, send it again")
            else:
                self._pending[key] = MaintenanceRequest(key, msg.adc_id, b.request_id, msg.receive_ts)
        else:  # format validation makes this unreachable; the table cell is Illegal
            raise FailSafe("TBL-REQ-DEFAULT", b.request_type)
        self._check_invariant()

    # ------------------------------------------------------- user decision
    def pending_maintenance(self) -> List[MaintenanceRequest]:
        """Return pending maintenance requests in receive-time order."""
        return sorted(self._pending.values(), key=lambda r: r.received_at)

    def decide_maintenance(self, auth: Authorisation, key: str, accept: bool) -> Result:
        """FR-REQ-04. Pre: auth issued for Fn.DECIDE_MAINTENANCE.
        Post: the ADC receives exactly one reply for the request, from the
        first technician to decide; a second decision is refused."""
        gate = self._mode.allows(ChangeKind.DECISION)
        if not gate:
            return self._refuse(auth.user_id, gate.reason)
        if auth.fn is not Fn.DECIDE_MAINTENANCE:
            return self._refuse(auth.user_id, "not permitted")
        self.tick(self._clock.now())
        req = self._pending.pop(key, None)
        if req is None:
            return self._refuse(auth.user_id, "request is not pending (already decided or timed out)")
        self._answered.add(key)
        decision = "accept" if accept else "reject"
        self._reply(req.adc_id, req.request_id, decision, f"decided by {auth.user_id}", by=auth.user_id)
        self._h.append(Category.USER_ACTION, "maintenance_decided",
                       {"target": key, "old": "pending", "new": decision}, adc_id=req.adc_id,
                       user_id=auth.user_id)
        self._check_invariant()
        return Result.success()

    # ---------------------------------------------------------------- timer
    def tick(self, now: float) -> None:
        """FR-REQ-05. Reject a maintenance request nobody decided within
        maintenance_decision_timeout_s."""
        for key, req in list(self._pending.items()):
            if now - req.received_at >= self._p.maintenance_decision_timeout_s:
                del self._pending[key]
                self._answered.add(key)
                self._reply(req.adc_id, req.request_id, "reject", "no decision")
        self._check_invariant()

    # -------------------------------------------------------------- restore
    def restore(self, events) -> None:
        """After a restart a maintenance request that never got a reply is
        rejected ("system restarted"); the ADC may ask again."""
        replied, asked = set(), []
        for rec, _ in events:
            if rec.category is Category.SYSTEM and rec.kind == "request_reply":
                replied.add(f"{rec.adc_id}/{rec.data['request_id']}")
            elif rec.category is Category.MESSAGE and rec.data.get("msg_type") == MsgKind.REQUEST.value \
                    and rec.data.get("outcome") == Outcome.ACCEPTED.value:
                c = rec.data["content"]
                asked.append((rec.adc_id, c["request_id"], c["request_type"]))
        self._answered |= replied
        for adc, rid, rtype in asked:
            key = f"{adc}/{rid}"
            self._answered.add(key)
            if rtype == "maintenance" and key not in replied:
                self._reply(adc, rid, "reject", "system restarted")

    # ------------------------------------------------------------ internals
    def _reply(self, adc_id: str, request_id: str, decision: str, reason: str,
               slot: Optional[int] = None, by: Optional[str] = None) -> None:
        self._send.send(adc_id, OutRequestReply(request_id, decision, reason, slot))
        self._h.append(Category.SYSTEM, "request_reply",
                       {"request_id": request_id, "slot": slot, "decision": decision, "reason": reason},
                       adc_id=adc_id, user_id=by)

    def _check_invariant(self) -> None:
        if set(self._pending) & self._answered:
            raise FailSafe("INV-REQ-1", "request both pending and answered")

    def _refuse(self, user_id: str, reason: str) -> Result:
        """Record a refused action without changing its result or application state."""
        self._h.append(Category.ACCESS, "action_refused",
                       {"module": "request_handler", "reason": reason}, user_id=user_id)
        return Result.refused(reason)
