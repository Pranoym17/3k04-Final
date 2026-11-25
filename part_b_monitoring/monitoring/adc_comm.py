"""ADC Communication Manager module.

Responsibility: turn the bytes of the ADC interface into validated typed
messages and back. It checks sender, authentication, format and duplicates,
labels replays Late, records every message, hands accepted messages to the
modules that registered for that kind, and queues what the Monitoring System
sends to each ADC.
Secret: the wire format (JSON field names, number formats), the
authentication scheme (HMAC-SHA256 over the canonical JSON) and the outbox.
Nothing outside this file knows a field name of the ADC messages. If Part A
changes the format, only `_decode`, `_SCHEMAS` and `_encode` change.

The transport (how bytes arrive) is hidden in ADC Transport Adapter.

D1 traceability: FR-COM-03, FR-COM-04, FR-COM-09, FR-COM-10, FR-COM-11 (send
side), FR-HIS-01, NFR-SEC-02, NFR-SEC-03, NFR-PRF-03, assumptions A2, A9, A11,
hazard H4.

State invariant I-COM: a message ID is in `_seen[adc]` only if a message with
that ID was accepted (Accepted or Late) for that ADC; a rejected message never
adds an ID; every received message produces exactly one MESSAGE event.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import math
import re
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from .clock import Clock
from .domain import (AdcMessage, AdcMode, AlarmClearBody, AlarmNoticeBody, Category, ConfigAckBody,
                     DispensingBody, FailSafe, InventoryBody, MsgKind, OutConfiguration, OutInventoryRequest,
                     OutRequestReply, Outcome, RequestBody, Severity, SlotReading, StatusBody)
from .history_store import HistoryStore
from .policy import Policy

Handler = Callable[[AdcMessage], None]

_ID = re.compile(r"^[A-Za-z0-9-]{1,32}$")
_TEXT = re.compile(r"^[A-Za-z0-9 _.,%+()/\-]{1,64}$")   # permitted characters for display text
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class FormatError(ValueError):
    """A received message does not satisfy the ADC format."""
    pass


def _int(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise FormatError(f"{name} must be a whole number from {lo} to {hi}")
    return v


def _text(v, name):
    if not isinstance(v, str) or not _TEXT.fullmatch(v):
        raise FormatError(f"{name} must be 1 to 64 characters from letters, digits, space and _ . , % + ( ) / -")
    return v


def _ident(v, name):
    if not isinstance(v, str) or not _ID.fullmatch(v):
        raise FormatError(f"{name} must be 1 to 32 letters, digits or -")
    return v


def _status(f):
    try:
        return StatusBody(AdcMode(f["mode"]))
    except ValueError:
        raise FormatError("mode must be Normal, Maintenance or Fault")


def _inventory(f):
    slots = f["slots"]
    if not isinstance(slots, list) or not 1 <= len(slots) <= 99:
        raise FormatError("slots must be a list of 1 to 99 entries")
    out, seen = [], set()
    for s in slots:
        if not isinstance(s, dict) or set(s) != {"slot", "medication", "quantity"}:
            raise FormatError("a slot entry needs exactly slot, medication and quantity")
        n = _int(s["slot"], 1, 99, "slot")
        if n in seen:
            raise FormatError("duplicate slot in report")
        seen.add(n)
        out.append(SlotReading(n, _text(s["medication"], "medication"), _int(s["quantity"], 0, 9999, "quantity")))
    return InventoryBody(tuple(out))


def _alarm_notice(f):
    sev = f["severity"]
    if sev not in ("low", "medium", "high"):
        raise FormatError("severity must be low, medium or high")
    return AlarmNoticeBody(_ident(f["alarm_id"], "alarm_id"), _text(f["alarm_type"], "alarm_type"),
                           Severity[sev.upper()])


def _alarm_clear(f):
    return AlarmClearBody(_ident(f["alarm_id"], "alarm_id"))


def _dispensing(f):
    return DispensingBody(_int(f["slot"], 1, 99, "slot"), _text(f["medication"], "medication"),
                          _int(f["quantity"], 1, 9999, "quantity"), _ident(f["adc_user_id"], "adc_user_id"))


def _request(f):
    rt = f["request_type"]
    rid = _ident(f["request_id"], "request_id")
    if rt == "refill":
        slots = f.get("slots")
        if not isinstance(slots, list) or not 1 <= len(slots) <= 8:
            raise FormatError("a refill request names 1 to 8 slots")
        s = tuple(_int(x, 1, 99, "slot") for x in slots)
        if len(set(s)) != len(s):
            raise FormatError("duplicate slot in request")
        return RequestBody(rid, rt, s)
    if rt == "maintenance":
        return RequestBody(rid, rt, ())
    raise FormatError("request_type must be refill or maintenance")


def _config_ack(f):
    return ConfigAckBody(_int(f["config_id"], 1, 10**9, "config_id"), _int(f["slot"], 1, 99, "slot"),
                         _int(f["threshold"], 0, 9999, "threshold"))


# Wire schema (assumption A2, our side of the interface): kind -> (required fields, parser).
# Fields outside this list are refused, so no patient identifier can be smuggled in (NFR-SEC-02).
_COMMON = {"adc_id", "message_id", "timestamp", "type", "auth"}
_SCHEMAS: Dict[str, Tuple[MsgKind, set, Callable]] = {
    "status": (MsgKind.STATUS, {"mode"}, _status),
    "inventory": (MsgKind.INVENTORY, {"slots"}, _inventory),
    "alarm_notice": (MsgKind.ALARM_NOTICE, {"alarm_id", "alarm_type", "severity"}, _alarm_notice),
    "alarm_clear": (MsgKind.ALARM_CLEAR, {"alarm_id"}, _alarm_clear),
    "dispensing": (MsgKind.DISPENSING, {"slot", "medication", "quantity", "adc_user_id"}, _dispensing),
    "request": (MsgKind.REQUEST, {"request_id", "request_type"}, _request),
    "config_ack": (MsgKind.CONFIG_ACK, {"config_id", "slot", "threshold"}, _config_ack),
}
_OPTIONAL = {"request": {"slots"}}


def _content(body) -> dict:
    """Wire-neutral description of an accepted message body, stored in the
    history. Keys are domain names, never wire field names, so a change of the
    wire format does not change the history format."""
    if body is None:
        return {}
    d = dataclasses.asdict(body)
    for k, v in list(d.items()):
        if isinstance(v, (AdcMode, Severity)):
            d[k] = v.name if isinstance(v, Severity) else v.value
    return d


def sign(key: str, fields: dict) -> str:
    """HMAC-SHA256 over the canonical JSON of `fields` (without "auth")."""
    canon = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(key.encode(), canon.encode(), hashlib.sha256).hexdigest()


class AdcCommunicationManager:
    """Validate ADC bytes and manage authenticated outbound messages."""
    def __init__(self, history: HistoryStore, clock: Clock, policy: Policy):
        """Initialise the module with its injected collaborators and empty state."""
        self._h = history
        self._clock = clock
        self._p = policy
        self._keys: Dict[str, str] = {}
        self._seen: Dict[str, set] = defaultdict(set)
        self._outbox: Dict[str, List[object]] = defaultdict(list)
        self._out_id: Dict[str, int] = defaultdict(int)
        self._handlers: Dict[MsgKind, List[Handler]] = defaultdict(list)
        self._invalid_listeners: List[Callable[[str], None]] = []

    # ---------------------------------------------------- wiring interface
    def register_handler(self, kind: MsgKind, fn: Handler) -> None:
        """A module that wants messages of `kind` registers here. Handlers are
        called in registration order. This is a callback, not a USES relation:
        this module does not import the modules that listen."""
        self._handlers[kind].append(fn)

    def add_invalid_listener(self, fn: Callable[[str], None]) -> None:
        """Register a callback for rejected messages from registered ADCs."""
        self._invalid_listeners.append(fn)

    def on_registry_change(self, event: str, adc_id: str, key: Optional[str]) -> None:
        """Called by Configuration Manager (registered, restored, deregistered)."""
        if event in ("registered", "restored") and key:
            self._keys[adc_id] = key
            if event == "registered":
                self._seen.pop(adc_id, None)     # decision D12: a re-registered ADC may restart its IDs
        elif event == "deregistered":
            self._keys.pop(adc_id, None)
            self._outbox.pop(adc_id, None)

    # -------------------------------------------------------------- inbound
    def receive(self, raw: bytes) -> bytes:
        """The inbound interface of the ADC link. Returns the encoded answer
        for the sender: {"result": ..., "reason": ..., "outbox": [...]}.

        Function table, rows evaluated in this order (first match wins):
          V1 not a JSON object, or adc_id/auth missing or ill-formed -> Rejected ("malformed" recorded)
          V2 adc_id not registered (never, or deregistered)          -> Rejected  (FR-COM-03)
          V3 auth does not match the registered key                  -> Rejected  (NFR-SEC-03)
          V4 type unknown or fields do not match the schema          -> Rejected  (FR-COM-04, NFR-SEC-02)
          V5 message_id already accepted for this ADC                -> Duplicate, discarded (FR-COM-10)
          V6 ADC time is more than late_limit_s before receive time  -> Accepted as Late (FR-COM-09)
          V7 otherwise                                               -> Accepted
        Every row records exactly one MESSAGE event (FR-HIS-01). The answer
        exposes no validation diagnostic; detailed reasons stay in history."""
        now = self._clock.now()
        claimed, fields, msg, outcome, reason = None, {}, None, Outcome.REJECTED, ""
        try:
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                raise FormatError("malformed")
            claimed = obj.get("adc_id")
            auth = obj.get("auth")
            if not isinstance(claimed, str) or not _ID.fullmatch(claimed) \
                    or not isinstance(auth, str) or not _HEX64.fullmatch(auth):
                raise FormatError("malformed")
        except (ValueError, UnicodeDecodeError, FormatError, RecursionError):
            return self._finish(now, None, {}, None, Outcome.REJECTED, "malformed", None, raw)
        fields = {k: v for k, v in obj.items() if k != "auth"}
        key = self._keys.get(claimed)
        if key is None:
            return self._finish(now, claimed, fields, None, Outcome.REJECTED, "unregistered ADC", None, raw)
        if not hmac.compare_digest(sign(key, fields), auth):
            return self._finish(now, claimed, fields, None, Outcome.REJECTED, "bad authentication code",
                                None, raw)
        try:
            parsed = self._decode(obj)
        except (FormatError, KeyError, TypeError, OverflowError) as e:
            return self._finish(now, claimed, fields, None, Outcome.REJECTED, f"bad format: {e}", None, raw)
        kind, message_id, adc_ts, body = parsed
        if message_id in self._seen[claimed]:
            return self._finish(now, claimed, fields, None, Outcome.DUPLICATE, "message ID already accepted",
                                kind, raw)
        late = now - adc_ts > self._p.late_limit_s
        msg = AdcMessage(kind, claimed, message_id, adc_ts, now, late, body)
        self._seen[claimed].add(message_id)
        return self._finish(now, claimed, fields, msg, Outcome.LATE if late else Outcome.ACCEPTED,
                            "replayed after an outage" if late else "", kind, raw)

    def _decode(self, obj: dict):
        t = obj.get("type")
        if not isinstance(t, str) or t not in _SCHEMAS:
            raise FormatError("unknown type")
        kind, required, parser = _SCHEMAS[t]
        allowed = _COMMON | required | _OPTIONAL.get(t, set())
        if set(obj) - allowed:
            raise FormatError("unexpected field(s)")
        if not (_COMMON | required) - _OPTIONAL.get(t, set()) <= set(obj):
            raise FormatError("a required field is missing")
        message_id = _int(obj["message_id"], 0, 10**12, "message_id")
        ts = obj["timestamp"]
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts) or ts < 0:
            raise FormatError("timestamp must be seconds since the epoch")
        return kind, message_id, float(ts), parser(obj)

    def _finish(self, now: float, claimed: Optional[str], fields: dict, msg: Optional[AdcMessage],
                outcome: Outcome, reason: str, kind: Optional[MsgKind], raw: bytes) -> bytes:
        registered = claimed in self._keys
        msg_type = kind.value if kind else fields.get("type")
        if not isinstance(msg_type, str) or msg_type not in _SCHEMAS:
            msg_type = None
        message_id = fields.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or not 0 <= message_id <= 10**12:
            message_id = None
        adc_ts = fields.get("timestamp")
        try:
            if isinstance(adc_ts, bool) or not isinstance(adc_ts, (int, float)) or not math.isfinite(adc_ts) or adc_ts < 0:
                adc_ts = None
        except OverflowError:
            adc_ts = None
        data = {"claimed_adc_id": claimed, "msg_type": msg_type,
                "message_id": message_id, "adc_ts": adc_ts,
                "receive_ts": now, "outcome": outcome.value, "reason": reason,
                "late": outcome is Outcome.LATE,
                "content": _content(msg.body) if msg is not None else {}}
        self._h.append(Category.MESSAGE, "adc_message", data, adc_id=claimed if registered else None, ts=now)
        if outcome in (Outcome.ACCEPTED, Outcome.LATE) and msg is not None:
            for fn in list(self._handlers.get(msg.kind, [])):
                try:
                    fn(msg)
                except FailSafe:
                    raise
                except Exception as e:  # one failing handler must not stop the others (decision D13)
                    self._h.append(Category.SYSTEM, "handler_error",
                                   {"handler": getattr(fn, "__qualname__", str(fn)), "error": repr(e)},
                                   adc_id=claimed)
        elif outcome is Outcome.REJECTED and registered:
            for fn in self._invalid_listeners:
                fn(claimed)
        public = {Outcome.ACCEPTED: "accepted", Outcome.LATE: "accepted", Outcome.DUPLICATE: "accepted",
                  Outcome.REJECTED: "rejected"}[outcome]
        shown = "rejected" if outcome is Outcome.REJECTED else ""
        answer = {"result": public, "reason": shown,
                  "outbox": self._drain(claimed) if (registered and outcome is not Outcome.REJECTED) else []}
        return json.dumps(answer, sort_keys=True).encode()

    # ------------------------------------------------------------- outbound
    def send(self, adc_id: str, out: object) -> None:
        """Queue an outbound message for one ADC (and only that ADC: FR-COM-05).
        Delivery happens with the next answer or poll."""
        if adc_id in self._keys:
            self._outbox[adc_id].append(out)

    def fetch_outbox(self, adc_id: str, ts: str, auth: str) -> bytes:
        """Authenticated poll. auth = HMAC(key, "outbox|<adc_id>|<ts>") with ts as sent. Returns
        {"messages": [...]} or {"error": ...}. A poll is not a message of the
        ADC interface: it never counts as proof of life."""
        key = self._keys.get(adc_id)
        now = self._clock.now()
        try:
            age = abs(now - float(ts))
        except (TypeError, ValueError):
            age = float("inf")
        if key is None or not isinstance(auth, str) or not _HEX64.fullmatch(auth) \
                or not math.isfinite(age) or age > self._p.outbox_poll_window_s \
                or not hmac.compare_digest(
                    hmac.new(key.encode(), f"outbox|{adc_id}|{ts}".encode(), hashlib.sha256).hexdigest(), auth):
            return json.dumps({"error": "rejected"}).encode()
        return json.dumps({"messages": self._drain(adc_id)}, sort_keys=True).encode()

    def pending_outbound(self, adc_id: str) -> int:
        """Count messages awaiting delivery to this ADC."""
        return len(self._outbox.get(adc_id, []))

    def _drain(self, adc_id: str) -> List[dict]:
        items, self._outbox[adc_id] = self._outbox.get(adc_id, []), []
        return [self._encode(adc_id, o) for o in items]

    def _encode(self, adc_id: str, out: object) -> dict:
        self._out_id[adc_id] += 1
        f = {"adc_id": adc_id, "message_id": self._out_id[adc_id], "timestamp": self._clock.now()}
        if isinstance(out, OutConfiguration):
            f.update(type="configuration", config_id=out.config_id, slot=out.slot, threshold=out.threshold)
        elif isinstance(out, OutRequestReply):
            f.update(type="request_reply", request_id=out.request_id, decision=out.decision,
                     reason=out.reason, slot=out.slot)
        elif isinstance(out, OutInventoryRequest):
            f.update(type="inventory_request")
        else:
            raise FailSafe("TBL-COM-OUTBOUND", f"unknown outbound message {out!r}")
        f["auth"] = sign(self._keys[adc_id], f)
        return f

    # -------------------------------------------------------------- restore
    def restore(self, events) -> None:
        """Rebuild the duplicate-detection index from accepted messages."""
        for rec, _ in events:
            if rec.category is Category.USER_ACTION and rec.kind == "adc_registered":
                self._seen.pop(rec.data["target"], None)
            elif rec.category is Category.MESSAGE and rec.adc_id \
                    and rec.data.get("outcome") in (Outcome.ACCEPTED.value, Outcome.LATE.value):
                self._seen[rec.adc_id].add(rec.data["message_id"])
