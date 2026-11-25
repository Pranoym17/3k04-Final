"""Inventory Supervisor module.

Responsibility: hold the latest reported quantity of every medication slot of
every registered ADC, the confirmed reorder threshold of each slot, and decide
which slots are Low.
Secret: how stock is represented and how "Low" is derived. If the rule
changes from "quantity <= threshold" to "days of supply", only this module
changes.

D1 traceability: FR-INV-01, FR-INV-02, NFR-SAF-01 (data side: report times),
scenario S1.

State invariant I-INV: quantity >= 0 and 0 <= threshold <= max_threshold for
every slot; a slot is Low exactly when quantity <= threshold.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .domain import (AdcMessage, Category, FailSafe, InventoryBody, MsgKind, Outcome, SlotView)
from .history_store import HistoryStore
from .policy import Policy


@dataclass
class _Slot:
    medication: str
    quantity: int
    adc_ts: float
    receive_ts: float


class InventorySupervisor:
    """Track reported slot quantities and confirmed thresholds."""
    def __init__(self, history: HistoryStore, policy: Policy):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._p = policy
        self._slots: Dict[str, Dict[int, _Slot]] = {}
        self._thresholds: Dict[str, Dict[int, int]] = {}
        self._last_applied_ts: Dict[str, float] = {}

    # ---------------------------------------------------------- inbound
    def on_inventory_report(self, msg: AdcMessage) -> None:
        """FR-INV-01. Pre: msg.kind is INVENTORY and msg passed validation.
        Post (not late, newer than the last applied report): the slots of
        msg.adc_id equal the reported slots (full report, A12).
        Post (late, or older than the last applied report): nothing changes,
        the ignored event is recorded (FR-HIS-05)."""
        if msg.kind is not MsgKind.INVENTORY:
            raise FailSafe("INV-INV-KIND", str(msg.kind))
        body: InventoryBody = msg.body
        last = self._last_applied_ts.get(msg.adc_id)
        if msg.late or (last is not None and msg.adc_ts < last):
            self._h.append(Category.SYSTEM, "ignored_event",
                           {"machine": "inventory", "event": "inventory report",
                            "reason": "late or older than the report already applied"},
                           adc_id=msg.adc_id)
            return
        self._slots[msg.adc_id] = {
            r.slot: _Slot(r.medication, r.quantity, msg.adc_ts, msg.receive_ts) for r in body.slots}
        self._last_applied_ts[msg.adc_id] = msg.adc_ts
        self._check_invariant()

    def apply_confirmed_threshold(self, adc_id: str, slot: int, value: int) -> None:
        """Called by Configuration Manager when the ADC acknowledged a change
        (FR-COM-06, A14). Pre: 0 <= value <= max_threshold."""
        if not 0 <= value <= self._p.max_threshold:
            raise FailSafe("INV-INV-2", f"threshold {value}")
        self._thresholds.setdefault(adc_id, {})[slot] = value
        self._check_invariant()

    def forget(self, adc_id: str) -> None:
        """An ADC was deregistered: drop its current view (events stay in history)."""
        self._slots.pop(adc_id, None)
        self._last_applied_ts.pop(adc_id, None)

    # ----------------------------------------------------------- queries
    def confirmed_threshold(self, adc_id: str, slot: int) -> int:
        """Return the confirmed threshold or its policy default."""
        return self._thresholds.get(adc_id, {}).get(slot, self._p.default_reorder_threshold)

    def has_slot(self, adc_id: str, slot: int) -> bool:
        """Return whether the ADC has reported the named slot."""
        return slot in self._slots.get(adc_id, {})

    def slots(self, adc_id: str) -> List[SlotView]:
        """Return sorted immutable slot views with report times and Low flags."""
        out = []
        for n, s in sorted(self._slots.get(adc_id, {}).items()):
            t = self.confirmed_threshold(adc_id, n)
            out.append(SlotView(adc_id, n, s.medication, s.quantity, t, s.quantity <= t,
                                s.adc_ts, s.receive_ts))
        return out

    def is_low(self, adc_id: str, slot: int) -> bool:
        """Return whether the slot quantity is at or below its threshold."""
        s = self._slots.get(adc_id, {}).get(slot)
        return s is not None and s.quantity <= self.confirmed_threshold(adc_id, slot)

    def low_slots(self, adc_id: str) -> List[SlotView]:
        """Return the ADC slots currently flagged Low."""
        return [v for v in self.slots(adc_id) if v.low]

    def low_count(self, adc_id: str) -> int:
        """Count the ADC slots currently flagged Low."""
        return len(self.low_slots(adc_id))

    def all_low_slots(self, adc_ids) -> List[SlotView]:
        """Return all Low slots across the supplied ADC IDs."""
        out: List[SlotView] = []
        for a in sorted(adc_ids):
            out.extend(self.low_slots(a))
        return out

    def last_report(self, adc_id: str) -> Optional[tuple]:
        """(adc_ts, receive_ts) of the report behind the current quantities."""
        slots = self._slots.get(adc_id)
        if not slots:
            return None
        s = next(iter(slots.values()))
        return s.adc_ts, s.receive_ts

    # ----------------------------------------------------------- restore
    def restore(self, events) -> None:
        """Rebuild thresholds and the last applied inventory report per ADC."""
        for rec, _ in events:
            if rec.category is Category.USER_ACTION and rec.kind == "adc_deregistered":
                self.forget(rec.adc_id)
            elif rec.category is Category.SYSTEM and rec.kind == "config_change" \
                    and rec.data.get("status") == "Confirmed":
                self._thresholds.setdefault(rec.adc_id, {})[rec.data["slot"]] = rec.data["new"]
            elif rec.category is Category.MESSAGE and rec.data.get("msg_type") == MsgKind.INVENTORY.value \
                    and rec.data.get("outcome") == Outcome.ACCEPTED.value:
                c = rec.data["content"]
                last = self._last_applied_ts.get(rec.adc_id)
                if last is not None and rec.data["adc_ts"] < last:
                    continue
                self._slots[rec.adc_id] = {
                    e["slot"]: _Slot(e["medication"], e["quantity"], rec.data["adc_ts"], rec.data["receive_ts"])
                    for e in c["slots"]}
                self._last_applied_ts[rec.adc_id] = rec.data["adc_ts"]
        self._check_invariant()

    def _check_invariant(self) -> None:
        for adc, slots in self._slots.items():
            for n, s in slots.items():
                if s.quantity < 0:
                    raise FailSafe("INV-INV-1", f"{adc}/{n}")
