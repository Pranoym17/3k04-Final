"""Domain Types module.

Responsibility: the shared vocabulary of the Monitoring System (states, modes,
roles, severities, message value objects and the Result type).

Secret: how domain values are represented in memory. The ADC wire format is NOT
here; it is hidden inside ADC Communication Manager. Every other module sees
only these typed values.

D1 traceability: domain model (Figure 2), state names in section 6.4, roles in
section 8.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Tuple


# ---------------------------------------------------------------- enumerations
class ConnState(Enum):
    """Connection states of a cabinet."""
    UNREGISTERED = "Unregistered"
    OFFLINE = "Offline"
    RECONNECTING = "Reconnecting"
    ONLINE = "Online"


class AdcMode(Enum):
    """Operating modes reported by a cabinet."""
    NORMAL = "Normal"
    MAINTENANCE = "Maintenance"
    FAULT = "Fault"


class SysMode(Enum):
    """Operating modes of the Monitoring System."""
    STARTING = "Starting"
    NORMAL = "Normal"
    MAINTENANCE = "Maintenance"
    FAULT = "Fault"


class Severity(Enum):
    """Ordered alarm severity levels."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @property
    def label(self) -> str:
        """Return a human-readable severity label."""
        return self.name.capitalize()


class AlarmState(Enum):
    """States of an alarm lifecycle."""
    RAISED = "Raised"
    ESCALATED = "Escalated"
    ACKNOWLEDGED = "Acknowledged"
    CLEARED = "Cleared"


class Origin(Enum):
    """The source responsible for raising an alarm."""
    ADC = "ADC"
    MONITORING = "Monitoring System"


class Role(Enum):
    """The single role assigned to each user account."""
    PHARMACIST = "Pharmacist"
    NURSE = "Nurse"
    TECHNICIAN = "Technician"
    ADMINISTRATOR = "Administrator"


class Fn(Enum):
    """User-visible functions that carry a role restriction (D1 section 8)."""
    VIEW_DASHBOARD = "view dashboard, inventory and alarms"
    ACK_ALARM = "acknowledge an alarm"
    CHANGE_THRESHOLD = "change a reorder threshold"
    VIEW_HISTORY = "view history and dispensing events"
    RUN_INVENTORY_REPORT = "run inventory report"
    RUN_ALARM_AVAIL_REPORTS = "run alarm and availability reports"
    DECIDE_MAINTENANCE = "decide maintenance requests"
    ADMIN = "register ADCs, manage accounts, view audit records, switch Maintenance mode"


class ChangeKind(Enum):
    """Kinds of user data change that System Mode Manager may refuse."""
    THRESHOLD = "threshold change"
    REGISTRATION = "ADC registration change"
    ACCOUNT = "account change"
    ACKNOWLEDGE = "alarm acknowledgement"
    DECISION = "maintenance decision"


class MsgKind(Enum):
    """Kinds of validated inbound ADC messages."""
    STATUS = "status"
    INVENTORY = "inventory"
    ALARM_NOTICE = "alarm_notice"
    ALARM_CLEAR = "alarm_clear"
    DISPENSING = "dispensing"
    REQUEST = "request"
    CONFIG_ACK = "config_ack"


class Outcome(Enum):
    """Recorded outcomes of inbound message validation."""
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    DUPLICATE = "Duplicate"
    LATE = "Late"


class Category(Enum):
    """Event categories in the History Store."""
    MESSAGE = "message"
    SYSTEM = "system"
    USER_ACTION = "user_action"
    ACCESS = "access"


# ------------------------------------------------------------------ result type
@dataclass(frozen=True)
class Result:
    """An operation outcome with a reason and optional return value."""
    ok: bool
    reason: str = ""
    value: Any = None

    def __bool__(self) -> bool:  # lets callers write "if result:"
        """Return whether the operation succeeded."""
        return self.ok

    @staticmethod
    def success(value: Any = None, reason: str = "") -> "Result":
        """Build a successful result with optional value and reason."""
        return Result(True, reason, value)

    @staticmethod
    def refused(reason: str) -> "Result":
        """Build a refused result without a return value."""
        return Result(False, reason, None)


class FailSafe(RuntimeError):
    """An internal invariant or a table cell marked Illegal was violated.

    The condition means the specification or the code is wrong, not the input.
    """

    def __init__(self, code: str, detail: str = ""):
        """Record the invariant failure code and optional detail."""
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# --------------------------------------------------------- authorisation token
@dataclass(frozen=True)
class Authorisation:
    """Issued only by Access Control. Proves that `user_id` (with `role`) is
    allowed to perform `fn` at the moment of issue."""
    user_id: str
    role: Role
    fn: Fn


# ------------------------------------------------------ inbound message values
@dataclass(frozen=True)
class SlotReading:
    """One medication quantity reported for an ADC slot."""
    slot: int
    medication: str
    quantity: int


@dataclass(frozen=True)
class StatusBody:
    """A validated cabinet operating mode."""
    mode: AdcMode


@dataclass(frozen=True)
class InventoryBody:
    """A validated complete collection of slot readings."""
    slots: Tuple[SlotReading, ...]


@dataclass(frozen=True)
class AlarmNoticeBody:
    """The identity, type and severity of an ADC alarm notice."""
    alarm_id: str
    alarm_type: str
    severity: Severity


@dataclass(frozen=True)
class AlarmClearBody:
    """The identity of the ADC alarm being cleared."""
    alarm_id: str


@dataclass(frozen=True)
class DispensingBody:
    """A dispensing event containing an ADC user ID and no patient field."""
    slot: int
    medication: str
    quantity: int
    adc_user_id: str


@dataclass(frozen=True)
class RequestBody:
    """A refill or maintenance request and its optional slots."""
    request_id: str
    request_type: str  # "refill" or "maintenance"
    slots: Tuple[int, ...] = ()


@dataclass(frozen=True)
class ConfigAckBody:
    """The configuration ID, slot and threshold acknowledged by an ADC."""
    config_id: int
    slot: int
    threshold: int


@dataclass(frozen=True)
class AdcMessage:
    """A message that passed registration, authentication, format and
    duplicate checks. `late` is True when it was replayed after an outage."""
    kind: MsgKind
    adc_id: str
    message_id: int
    adc_ts: float
    receive_ts: float
    late: bool
    body: Any


# ----------------------------------------------------- outbound message values
@dataclass(frozen=True)
class OutConfiguration:
    """A threshold command awaiting delivery to its owning ADC."""
    config_id: int
    slot: int
    threshold: int


@dataclass(frozen=True)
class OutRequestReply:
    """A request decision, reason and optional slot."""
    request_id: str
    decision: str  # "accept" or "reject"
    reason: str
    slot: Optional[int] = None


@dataclass(frozen=True)
class OutInventoryRequest:
    """A command asking an ADC to report its inventory."""
    pass


# ----------------------------------------------------------- read-only records
@dataclass(frozen=True)
class SlotView:
    """An immutable inventory value with threshold and report times."""
    adc_id: str
    slot: int
    medication: str
    quantity: int
    threshold: int
    low: bool
    adc_ts: float       # ADC timestamp of the report that supplied the quantity
    receive_ts: float   # Monitoring System receive time of that report


@dataclass(frozen=True)
class HealthSnapshot:
    """An immutable connection view with derived Stale status."""
    adc_id: str
    state: ConnState
    stale: bool
    last_valid_receive: Optional[float]
    adc_mode: Optional[AdcMode]


@dataclass(frozen=True)
class AlarmView:
    """An immutable alarm view with lifecycle times and acknowledgement."""
    key: str                # "<adc_id>/<alarm_id>"
    adc_id: str
    alarm_id: str
    alarm_type: str
    severity: Severity
    origin: Origin
    state: AlarmState
    adc_ts: Optional[float]
    receive_ts: float
    acknowledged_by: Optional[str]
    acknowledged_at: Optional[float]
    cleared_at: Optional[float]


@dataclass(frozen=True)
class EventRecord:
    """A recorded history event without its secret replay fields."""
    seq: int
    ts: float
    category: Category
    kind: str
    adc_id: Optional[str]
    user_id: Optional[str]
    data: dict = field(default_factory=dict)
