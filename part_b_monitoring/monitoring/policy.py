"""Policy module (behaviour hiding).

Responsibility: hold every timing constant and numeric limit of the system.
Secret: the VALUES of those constants. No other module contains a literal such
as 30 or 120; each reads the value it needs from a Policy object.

Anticipated change: "the regulator demands a longer alarm escalation time",
"the status interval of Part A changes", "retention becomes 15 years".
D1 traceability: assumptions A1, A9, A10, A15 and every requirement that
quotes a time (FR-CHM-01, FR-CHM-05, FR-CHM-06, FR-COM-07, FR-ALM-04,
FR-REQ-05, FR-AUT-03, FR-MOD-04, NFR-SEC-01, NFR-PRF-01 to NFR-PRF-04).
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Policy:
    # --- ADC link (A1, A9, FR-CHM-01, FR-CHM-05, FR-CHM-06, FR-COM-09)
    """Immutable timing, retention and capacity defaults for the system."""
    status_interval_s: float = 10.0           # A1: ADC sends status this often
    offline_timeout_s: float = 30.0           # FR-CHM-01: silence before Offline
    reconnect_timeout_s: float = 30.0         # FR-CHM-05/06: wait for inventory
    late_limit_s: float = 30.0                # D2 decision D3: age that makes a message Late
    outbox_poll_window_s: float = 300.0       # D2 decision D9: accepted clock gap for polling
    # --- alarms (FR-ALM-04, A15)
    escalation_after_s: float = 120.0
    # --- configuration (FR-COM-07)
    threshold_ack_timeout_s: float = 60.0
    default_reorder_threshold: int = 5        # D2 decision D5
    max_threshold: int = 9999
    # --- requests (FR-REQ-05)
    maintenance_decision_timeout_s: float = 300.0
    # --- access control (FR-AUT-03, NFR-SEC-01)
    lockout_attempts: int = 5
    lockout_duration_s: float = 900.0
    session_idle_s: float = 600.0
    # --- modes and storage (FR-MOD-04, NFR-REL-02, D2 decision D8)
    fault_test_interval_s: float = 10.0
    heartbeat_interval_s: float = 60.0
    pending_write_limit: int = 10_000
    retention_years: int = 10
    # --- capacity (NFR-PRF-03)
    max_adcs: int = 50
    # --- time between ticks of the scheduler
    tick_interval_s: float = 0.5

    def scaled(self, divisor: float) -> "Policy":
        """Shrink operational deadlines and the heartbeat for rehearsal.
        The scheduler cadence and signed-poll freshness window stay unchanged."""
        if divisor <= 0:
            raise ValueError("divisor must be positive")
        d = divisor
        return replace(
            self,
            status_interval_s=self.status_interval_s / d,
            offline_timeout_s=self.offline_timeout_s / d,
            reconnect_timeout_s=self.reconnect_timeout_s / d,
            late_limit_s=self.late_limit_s / d,
            escalation_after_s=self.escalation_after_s / d,
            threshold_ack_timeout_s=self.threshold_ack_timeout_s / d,
            maintenance_decision_timeout_s=self.maintenance_decision_timeout_s / d,
            lockout_duration_s=self.lockout_duration_s / d,
            session_idle_s=self.session_idle_s / d,
            fault_test_interval_s=self.fault_test_interval_s / d,
            heartbeat_interval_s=self.heartbeat_interval_s / d,
        )
