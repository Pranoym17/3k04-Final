"""Report Generator module.

Responsibility: build the three reports (inventory, alarm, availability) from
the current state and the history, and turn a report into CSV.
Secret: report content and layout. Adding a column, a new report or a PDF
format changes this module only.

D1 traceability: FR-RPT-01 to FR-RPT-03, scenario S1, NFR-USA-03 (data side).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .clock import Clock
from .domain import Authorisation, Category, ConnState, Fn, Result
from .history_store import HistoryStore


@dataclass(frozen=True)
class Report:
    """A generated report represented as a title, columns and rows."""
    title: str
    generated_at: float
    columns: Sequence[str]
    rows: Sequence[Sequence]


class ReportGenerator:
    """Generate inventory, alarm, availability and dispensing reports."""
    def __init__(self, history: HistoryStore, clock: Clock, inventory, health, alarms, registry):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._clock = clock
        self._inv = inventory
        self._health = health
        self._alarms = alarms
        self._registry = registry    # Configuration Manager (registered_ids)

    # ------------------------------------------------------------ FR-RPT-01
    def inventory_report(self, auth: Authorisation) -> Result:
        """Every Low slot of every registered ADC. Rows of an ADC that is
        Offline or Reconnecting are labelled Stale (FR-CHM-03)."""
        if auth.fn is not Fn.RUN_INVENTORY_REPORT:
            return self._refuse(auth.user_id, "not permitted")
        rows = []
        for v in self._inv.all_low_slots(self._registry.registered_ids()):
            stale = self._health.is_stale(v.adc_id)
            rows.append([v.adc_id, v.slot, v.medication, v.quantity, v.threshold,
                         "Stale" if stale else "Current", v.adc_ts])
        return Result.success(Report("Inventory report: Low slots", self._clock.now(),
                                     ["ADC", "Slot", "Medication", "Quantity", "Threshold",
                                      "Data", "Last ADC report time"], rows))

    # ------------------------------------------------------------ FR-RPT-02
    def alarm_report(self, auth: Authorisation, start: float, end: float,
                     adc_id: Optional[str] = None) -> Result:
        """Alarms received in [start, end], by ADC, type and severity, with
        time to acknowledgement (seconds) or "not acknowledged"."""
        if auth.fn is not Fn.RUN_ALARM_AVAIL_REPORTS:
            return self._refuse(auth.user_id, "not permitted")
        if start > end:
            return self._refuse(auth.user_id, "the start of the range is after its end")
        alarms = self._alarms.alarms_between(start, end, adc_id)
        alarms.sort(key=lambda a: (a.adc_id, a.alarm_type, -a.severity.value, a.receive_ts))
        rows = []
        for a in alarms:
            tta = (f"{a.acknowledged_at - a.receive_ts:.0f} s" if a.acknowledged_at is not None
                   else "not acknowledged")
            rows.append([a.adc_id, a.alarm_id, a.alarm_type, a.severity.label, a.origin.value,
                         a.receive_ts, a.state.value, tta])
        return Result.success(Report("Alarm report", self._clock.now(),
                                     ["ADC", "Alarm ID", "Type", "Severity", "Origin", "Received",
                                      "State", "Time to acknowledgement"], rows))

    # ------------------------------------------------------------ FR-RPT-03
    def availability_report(self, auth: Authorisation, start: float, end: float) -> Result:
        """Per ADC: number of Offline periods overlapping [start, end] and the
        total time Offline inside it. An Offline period begins when the ADC
        becomes Offline and ends when it becomes Online again or is
        deregistered; time spent Reconnecting counts as Offline (decision D11)."""
        if auth.fn is not Fn.RUN_ALARM_AVAIL_REPORTS:
            return self._refuse(auth.user_id, "not permitted")
        if start > end:
            return self._refuse(auth.user_id, "the start of the range is after its end")
        horizon = min(end, self._clock.now())
        events = self._h.query(category=Category.SYSTEM, kind="connection_change", end=horizon)
        periods: Dict[str, List[List[Optional[float]]]] = {}
        for e in events:
            adc = e.adc_id
            to = ConnState(e.data["to"])
            plist = periods.setdefault(adc, [])
            open_ = plist and plist[-1][1] is None
            if to is ConnState.OFFLINE and not open_:
                plist.append([e.ts, None])
            elif to in (ConnState.ONLINE, ConnState.UNREGISTERED) and open_:
                plist[-1][1] = e.ts
        rows = []
        for adc in sorted(set(periods) | set(self._registry.registered_ids())):
            count, total = 0, 0.0
            for s, e in periods.get(adc, []):
                e = horizon if e is None else min(e, horizon)
                overlap = min(e, horizon) - max(s, start)
                if overlap > 0 or (start <= s <= horizon and e == s):
                    count += 1
                    total += max(overlap, 0.0)
            rows.append([adc, count, f"{total / 60:.1f} min"])
        return Result.success(Report("Availability report", self._clock.now(),
                                     ["ADC", "Offline periods", "Total time Offline"], rows))

    # ------------------------------------------------------------ FR-HIS-04
    def dispensing_list(self, auth: Authorisation, adc_id: str, start: float, end: float) -> Result:
        """Dispensing events one ADC reported in [start, end]: slot, medication,
        quantity, ADC user ID and ADC time; replays are marked Late. No patient
        identifier exists to show (A6, NFR-SEC-02)."""
        if auth.fn is not Fn.VIEW_HISTORY:
            return self._refuse(auth.user_id, "not permitted")
        if start > end:
            return self._refuse(auth.user_id, "the start of the range is after its end")
        rows = []
        for e in self._h.query(adc_id=adc_id, category=Category.MESSAGE, start=start, end=end):
            d = e.data
            if d.get("msg_type") == "dispensing" and d.get("outcome") in ("Accepted", "Late"):
                c = d["content"]
                rows.append([c["slot"], c["medication"], c["quantity"], c["adc_user_id"],
                             d["adc_ts"], "Late" if d.get("late") else ""])
        return Result.success(Report(f"Dispensing events of {adc_id}", self._clock.now(),
                                     ["Slot", "Medication", "Quantity", "ADC user", "ADC time", "Marked"], rows))

    # ---------------------------------------------------------------- format
    @staticmethod
    def to_csv(report: Report) -> str:
        """Encode a report as CSV including its column headings."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(report.columns)
        w.writerows(report.rows)
        return buf.getvalue()

    def _refuse(self, user_id: str, reason: str) -> Result:
        """Record a refused report request without generating report data."""
        self._h.append(Category.ACCESS, "action_refused",
                       {"module": "report_generator", "reason": reason}, user_id=user_id)
        return Result.refused(reason)
