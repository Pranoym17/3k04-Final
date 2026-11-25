"""Configuration Manager module.

Responsibility: the ADC registry (which cabinets are known, with which key)
and the life of every reorder-threshold change: Unconfirmed, Confirmed,
Failed. It sends a change only to the ADC that owns the slot and never
assumes an uncertain command succeeded.
Secret: how the registry is kept and how a configuration change is tracked.
Distributing a new kind of setting (not only reorder thresholds) changes this
module and the outbound message list of the ADC interface.

D1 traceability: FR-COM-01, FR-COM-05 to FR-COM-08, FR-COM-12, FR-MOD-06
(refusal side), A3, A5, A14, hazards H4 and H5.

State invariant I-CFG: at most one Unconfirmed change per (ADC, slot); a
Confirmed value reaches Inventory Supervisor only through an acknowledgement
(or a late acknowledgement of a Failed change); config IDs strictly increase.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .clock import Clock
from .domain import (AdcMessage, Authorisation, Category, ChangeKind, ConfigAckBody, ConnState,
                     FailSafe, Fn, MsgKind, OutConfiguration, Result, Severity)
from .history_store import HistoryStore
from .policy import Policy

_ADC_ID = re.compile(r"^[A-Za-z0-9-]{1,32}$")


@dataclass
class ThresholdChange:
    """A requested threshold value and its confirmation status."""
    config_id: int
    adc_id: str
    slot: int
    old: int
    new: int
    status: str            # Unconfirmed, Confirmed, Failed, Superseded
    requested_by: str
    requested_at: float


class ConfigurationManager:
    """Own the ADC registry and threshold confirmation protocol."""
    def __init__(self, history: HistoryStore, clock: Clock, policy: Policy, mode,
                 sender, health, inventory, alarms):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._clock = clock
        self._p = policy
        self._mode = mode            # System Mode Manager
        self._send = sender          # ADC Communication Manager (send only)
        self._health = health        # Cabinet Health Monitor (state_of)
        self._inv = inventory        # Inventory Supervisor
        self._alarms = alarms        # Alarm Manager
        self._registered: set = set()
        self._changes: Dict[int, ThresholdChange] = {}
        self._next_id = 1
        self._listeners: List[Callable[[str, str, Optional[str]], None]] = []
        self._keys_restored: Dict[str, Optional[str]] = {}

    # ------------------------------------------------------------ registry
    def add_registry_listener(self, fn: Callable[[str, str, Optional[str]], None]) -> None:
        """fn(event, adc_id, key). event is "registered", "restored" or "deregistered".
        A listener is not a USES relation: this module does not know who listens."""
        self._listeners.append(fn)

    def is_registered(self, adc_id: str) -> bool:
        """Return whether the ADC is currently registered."""
        return adc_id in self._registered

    def registered_ids(self) -> List[str]:
        """Return registered ADC IDs in sorted order."""
        return sorted(self._registered)

    def register_adc(self, auth: Authorisation, adc_id: str, key: Optional[str] = None) -> Result:
        """FR-COM-01. Function table (rows in order):
          R1 mode refuses registration changes   -> refused (FR-MOD-06)
          R2 auth.fn is not ADMIN                -> refused
          R3 adc_id is not 1 to 32 letters, digits or -  -> refused
          R4 adc_id already registered           -> refused
          R5 registered count >= max_adcs        -> refused (NFR-PRF-03 capacity)
          R6 otherwise                           -> registered; Result.value is the ADC key."""
        gate = self._mode.allows(ChangeKind.REGISTRATION)
        if not gate:
            return self._refuse(auth.user_id, gate.reason)
        if auth.fn is not Fn.ADMIN:
            return self._refuse(auth.user_id, "not permitted")
        if not isinstance(adc_id, str) or not _ADC_ID.fullmatch(adc_id):
            return self._refuse(auth.user_id, "ADC ID must be 1 to 32 letters, digits or -")
        if adc_id in self._registered:
            return self._refuse(auth.user_id, "ADC already registered")
        if len(self._registered) >= self._p.max_adcs:
            return self._refuse(auth.user_id, f"capacity of {self._p.max_adcs} ADCs reached")
        key = key or secrets.token_hex(16)
        self._registered.add(adc_id)
        self._h.append(Category.USER_ACTION, "adc_registered",
                       {"target": adc_id, "old": "unregistered", "new": "registered"},
                       adc_id=adc_id, user_id=auth.user_id, secret={"key": key})
        for fn in list(self._listeners):
            fn("registered", adc_id, key)
        return Result.success(key)

    def deregister_adc(self, auth: Authorisation, adc_id: str) -> Result:
        """FR-COM-12. Post: later messages from the ADC are rejected, its
        recorded events stay available, its pending changes are Failed."""
        gate = self._mode.allows(ChangeKind.REGISTRATION)
        if not gate:
            return self._refuse(auth.user_id, gate.reason)
        if auth.fn is not Fn.ADMIN:
            return self._refuse(auth.user_id, "not permitted")
        if adc_id not in self._registered:
            return self._refuse(auth.user_id, "ADC is not registered")
        self._registered.discard(adc_id)
        for c in self._changes.values():
            if c.adc_id == adc_id and c.status == "Unconfirmed":
                c.status = "Failed"
                self._record_change(c, "ADC deregistered")
        self._h.append(Category.USER_ACTION, "adc_deregistered",
                       {"target": adc_id, "old": "registered", "new": "unregistered"},
                       adc_id=adc_id, user_id=auth.user_id)
        for fn in list(self._listeners):
            fn("deregistered", adc_id, None)
        return Result.success()

    # ----------------------------------------------------------- thresholds
    def request_threshold_change(self, auth: Authorisation, adc_id: str, slot: int,
                                 new_value) -> Result:
        """FR-COM-05, FR-COM-06, FR-COM-08, FR-MOD-06, NFR-AUD-01.
        Pre: auth issued for Fn.CHANGE_THRESHOLD.
        Function table (rows in order):
          T1 mode refuses threshold changes (Maintenance, Fault, Starting) -> refused
          T2 auth.fn is not CHANGE_THRESHOLD                               -> refused
          T3 adc_id not registered                                          -> refused
          T4 ADC state is not Online                                        -> refused, state named
          T5 slot not reported by that ADC                                  -> refused
          T6 new_value is not an integer in 0..max_threshold               -> refused
          T7 new_value equals the confirmed value, none pending             -> refused
          T8 otherwise -> change created Unconfirmed, sent ONLY to adc_id,
             older Unconfirmed change of the slot is Superseded, user action recorded.
        Post (T8): confirmed threshold is unchanged until the ADC acknowledges (A14)."""
        gate = self._mode.allows(ChangeKind.THRESHOLD)
        if not gate:
            return self._refuse(auth.user_id, gate.reason)
        if auth.fn is not Fn.CHANGE_THRESHOLD:
            return self._refuse(auth.user_id, "not permitted")
        if adc_id not in self._registered:
            return self._refuse(auth.user_id, "unknown ADC")
        state = self._health.state_of(adc_id)
        if state is not ConnState.ONLINE:
            return self._refuse(auth.user_id, f"ADC is {state.value}, not Online")
        if not self._inv.has_slot(adc_id, slot):
            return self._refuse(auth.user_id, "unknown slot")
        if isinstance(new_value, bool) or not isinstance(new_value, int) \
                or not 0 <= new_value <= self._p.max_threshold:
            return self._refuse(auth.user_id, f"threshold must be a whole number from 0 to {self._p.max_threshold}")
        old = self._inv.confirmed_threshold(adc_id, slot)
        pending = [c for c in self._changes.values()
                   if c.adc_id == adc_id and c.slot == slot and c.status == "Unconfirmed"]
        if new_value == old and not pending:
            return self._refuse(auth.user_id, "value is already the confirmed threshold")
        for c in pending:
            c.status = "Superseded"
            self._record_change(c, "replaced by a newer change")
        ch = ThresholdChange(self._next_id, adc_id, slot, old, new_value, "Unconfirmed",
                             auth.user_id, self._clock.now())
        self._next_id += 1
        self._changes[ch.config_id] = ch
        self._h.append(Category.USER_ACTION, "threshold_change",
                       {"target": f"{adc_id}/slot {slot}", "old": old, "new": new_value,
                        "config_id": ch.config_id}, adc_id=adc_id, user_id=auth.user_id)
        self._record_change(ch, "requested")
        self._send.send(adc_id, OutConfiguration(ch.config_id, slot, new_value))
        self._check_invariant()
        return Result.success(ch)

    def on_config_ack(self, msg: AdcMessage) -> None:
        """FR-COM-06. Function table:
          C1 unknown config_id, wrong ADC, or slot/value differ -> ignored, recorded (FR-HIS-05)
          C2 change Unconfirmed -> Confirmed, Inventory Supervisor updated, threshold alarm cleared
          C3 change Failed      -> Confirmed (the ADC holds the value, A14), alarm cleared
          C4 change Confirmed or Superseded -> ignored, recorded."""
        if msg.kind is not MsgKind.CONFIG_ACK:
            raise FailSafe("INV-CFG-KIND", str(msg.kind))
        b: ConfigAckBody = msg.body
        c = self._changes.get(b.config_id)
        if c is None or c.adc_id != msg.adc_id or c.slot != b.slot or c.new != b.threshold:
            self._ignored(msg.adc_id, "configuration acknowledgement", "no matching change")
            return
        if c.status in ("Confirmed", "Superseded"):
            self._ignored(msg.adc_id, "configuration acknowledgement", f"change already {c.status}")
            return
        c.status = "Confirmed"
        self._inv.apply_confirmed_threshold(c.adc_id, c.slot, c.new)
        self._alarms.clear_monitoring_alarm(c.adc_id, "threshold", "change confirmed",
                                            qualifier=str(c.slot))
        self._record_change(c, "acknowledged by ADC")
        self._check_invariant()

    def tick(self, now: float) -> None:
        """FR-COM-07. An Unconfirmed change older than threshold_ack_timeout_s
        becomes Failed and a medium-severity alarm is raised. The last
        confirmed value stays in force (A14)."""
        for c in list(self._changes.values()):
            if c.status == "Unconfirmed" and now - c.requested_at >= self._p.threshold_ack_timeout_s:
                c.status = "Failed"
                self._record_change(c, "no acknowledgement in time")
                self._alarms.raise_monitoring_alarm(c.adc_id, "threshold", Severity.MEDIUM,
                                                    qualifier=str(c.slot))

    def changes(self, adc_id: Optional[str] = None) -> List[ThresholdChange]:
        """List threshold changes in ID order, optionally for one ADC."""
        return [c for c in sorted(self._changes.values(), key=lambda x: x.config_id)
                if adc_id is None or c.adc_id == adc_id]

    def latest_change(self, adc_id: str, slot: int) -> Optional[ThresholdChange]:
        """Return the latest change for the slot, or None."""
        mine = [c for c in self.changes(adc_id) if c.slot == slot]
        return mine[-1] if mine else None

    # -------------------------------------------------------------- restore
    def restore(self, events) -> None:
        """Rebuild the registry and threshold changes from replayed events."""
        for rec, secret in events:
            if rec.category is Category.USER_ACTION and rec.kind == "adc_registered":
                self._registered.add(rec.data["target"])
                self._keys_restored[rec.data["target"]] = secret.get("key")
            elif rec.category is Category.USER_ACTION and rec.kind == "adc_deregistered":
                self._registered.discard(rec.data["target"])
            elif rec.category is Category.SYSTEM and rec.kind == "config_change":
                d = rec.data
                cid = d["config_id"]
                if cid not in self._changes:
                    self._changes[cid] = ThresholdChange(cid, rec.adc_id, d["slot"], d["old"], d["new"],
                                                         d["status"], d.get("requested_by", ""), rec.ts)
                else:
                    self._changes[cid].status = d["status"]
                self._next_id = max(self._next_id, cid + 1)
        for adc in sorted(self._registered):
            key = self._keys_restored.get(adc)
            for fn in list(self._listeners):
                fn("restored", adc, key)
        self._check_invariant()

    # ------------------------------------------------------------- internals
    def _record_change(self, c: ThresholdChange, why: str) -> None:
        self._h.append(Category.SYSTEM, "config_change",
                       {"config_id": c.config_id, "slot": c.slot, "old": c.old, "new": c.new,
                        "status": c.status, "requested_by": c.requested_by, "reason": why},
                       adc_id=c.adc_id)

    def _ignored(self, adc_id: str, event: str, reason: str) -> None:
        self._h.append(Category.SYSTEM, "ignored_event",
                       {"machine": "configuration", "event": event, "reason": reason}, adc_id=adc_id)

    def _check_invariant(self) -> None:
        seen = set()
        for c in self._changes.values():
            if c.status == "Unconfirmed":
                k = (c.adc_id, c.slot)
                if k in seen:
                    raise FailSafe("INV-CFG-1", f"{k}")
                seen.add(k)

    def _refuse(self, user_id: str, reason: str) -> Result:
        """Record a refused action without changing its result or application state."""
        self._h.append(Category.ACCESS, "action_refused",
                       {"module": "config_manager", "reason": reason}, user_id=user_id)
        return Result.refused(reason)
