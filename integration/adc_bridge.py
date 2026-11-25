"""Monitoring Link Adapter (Deliverable 3 integration, ADC side).

Responsibility: carry Part A's outbound queue to the Monitoring System (Part B)
and carry Part B's outbound messages back into Part A.
Secret: the translation between the two wire formats. Part A speaks
{"v","id","kind","payload"} (adc/decisions/message_codec.py). Part B speaks the
signed JSON of monitoring/adc_comm.py over HTTP (monitoring/web/adc_gateway.py).
Neither part imports this file and neither part knows the other's format.

Rules this adapter keeps (see integration/INTERFACE.md and DECISIONS.md):
  * Part A's OutboundQueue stays the only store of unsent messages. A Part A
    message is acknowledged (through AdcApplication.monitor_response) only after
    Part B answered "accepted" for every Part B message made from it.
  * A Part A message gets its Part B message_id and timestamp once, when first seen,
    so a retry after a lost answer is a Duplicate at Part B, never a second event.
  * The timestamp is the wall clock when the adapter first saw the message, so a
    message replayed after an outage carries its old time and Part B records it Late.
  * A rejected message is never acknowledged. The queue holds and the count grows
    on the ADC banner, which is visible, rather than losing a safety message.
  * Nothing is acknowledged locally unless Part B answered in the same cycle,
    because monitor_response also tells Part A's link state that Part B is alive.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

# Part A mode names to Part B mode names. STARTUP has no Part B equivalent.
_MODE = {"NORMAL": "Normal", "MAINTENANCE": "Maintenance", "FAULT": "Fault"}
# Part B epoch for message IDs, so a restarted ADC does not reuse IDs Part B has seen.
_ID_EPOCH = 1_700_000_000


def sign(key: str, fields: dict) -> str:
    """HMAC-SHA256 over canonical JSON. Our own copy, written from INTERFACE.md."""
    canon = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(key.encode(), canon.encode(), hashlib.sha256).hexdigest()


class LinkDown(Exception):
    """The Monitoring System could not be reached."""


class HttpTransport:
    """Real transport: POST /adc/messages and GET /adc/outbox over HTTP or HTTPS."""

    def __init__(self, base_url: str, insecure: bool = False, timeout: float = 3.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._ctx = ssl._create_unverified_context() if insecure else None

    def post(self, raw: bytes) -> dict:
        req = urllib.request.Request(self.base + "/adc/messages", data=raw, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:      # 400 carries {"result": "rejected"}
            body = e.read()
            try:
                return json.loads(body or b"{}")
            except ValueError:
                raise LinkDown(f"HTTP {e.code}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LinkDown(e.__class__.__name__)

    def get_outbox(self, adc_id: str, ts: str, auth: str) -> dict:
        url = self.base + "/adc/outbox?" + urllib.parse.urlencode({"adc_id": adc_id, "ts": ts, "auth": auth})
        try:
            with urllib.request.urlopen(url, timeout=self.timeout, context=self._ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError:
            return {"messages": []}
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LinkDown(e.__class__.__name__)


class MonitoringLink:
    """One ADC's link to the Monitoring System. Call pump() periodically."""

    def __init__(self, app, adc_id: str, key: str, transport,
                 wall_clock: Callable[[], float] = time.time,
                 status_interval_s: float = 10.0,
                 slot_of: Optional[Callable[[str], int]] = None):
        self.app = app
        self.adc_id = adc_id
        self.key = key
        self.transport = transport
        self.now = wall_clock
        self.status_interval_s = status_interval_s
        self.slot_of = slot_of or (lambda drawer: int(drawer.lstrip("Dd")))
        self._last_id = 0                  # see _next_id
        self._in = 0                       # counter for inbound IDs given to Part A
        self._cache: Dict[str, List[dict]] = {}   # Part A msg_id -> Part B fields (unsigned)
        self._accepted: set = set()        # Part B message_ids answered "accepted"
        self._mode: Optional[str] = None   # Part B mode name, from Part A MODE_CHANGE
        self._alarms: Dict[str, List[str]] = {}  # drawer -> alarm_ids raised and not cleared
        self._last_status = None
        self._was_down = True              # first contact behaves like a reconnect
        self.cut = False                   # demo switch: simulate a pulled cable
        self.thresholds: Dict[int, int] = {}      # held here, see DECISIONS.md D3-06
        self.log: List[str] = []
        self.rejected: List[dict] = []
        self.lock = threading.RLock()

    # ------------------------------------------------------------ helpers
    def _say(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {text}")
        del self.log[:-200]

    def _next_id(self) -> int:
        """Milliseconds since _ID_EPOCH, strictly increasing. Part B remembers every
        accepted ID per ADC for ever and accepts 0..10**12, which lasts until 2055.
        A restarted ADC starts above every ID it used before, unless it averaged more
        than 1000 messages a second or its wall clock was set back."""
        self._last_id = max(self._last_id + 1, int((self.now() - _ID_EPOCH) * 1000))
        return self._last_id

    def _own_fields(self, mtype: str, **extra) -> dict:
        f = {"adc_id": self.adc_id, "message_id": self._next_id(),
             "timestamp": round(self.now(), 3), "type": mtype}
        f.update(extra)
        return f

    def _inventory_fields(self, message_id: Optional[int] = None, ts: Optional[float] = None) -> dict:
        slots = [{"slot": self.slot_of(drawer), "medication": item, "quantity": qty}
                 for item, (drawer, qty, _rev) in sorted(self.app.inventory_snapshot().items())]
        slots.sort(key=lambda s: s["slot"])
        if message_id is None:
            return self._own_fields("inventory", slots=slots)
        return {"adc_id": self.adc_id, "message_id": message_id, "timestamp": ts,
                "type": "inventory", "slots": slots}

    def _user_of(self, tx_id: str) -> str:
        for r in reversed(self.app.audit_tail(500)):
            if r.get("tx_id") == tx_id and r.get("user_id"):
                return r["user_id"]
        return "unknown"

    def _to_part_a(self, kind: str, payload: dict) -> str:
        self._in += 1
        wire = json.dumps({"v": 1, "id": "IN-%d" % self._in, "kind": kind, "payload": payload})
        return self.app.monitor_response(wire)

    # -------------------------------------------------------- translation
    def _translate(self, msg_id: str, wire: str) -> List[dict]:
        """Part A message -> zero or one Part B message (fields without auth)."""
        m = json.loads(wire)
        kind, p = m["kind"], m.get("payload", {})
        mid, ts = self._next_id(), round(self.now(), 3)
        base = {"adc_id": self.adc_id, "message_id": mid, "timestamp": ts}
        if kind == "MODE_CHANGE":
            mode = _MODE.get(p.get("to"))
            if mode is None:
                return []
            self._mode = mode
            return [dict(base, type="status", mode=mode)]
        if kind == "INVENTORY_UPDATE":
            return [self._inventory_fields(mid, ts)]
        if kind == "ALARM":
            drawer = p.get("drawer", "")
            self._alarms.setdefault(drawer, []).append(msg_id)
            return [dict(base, type="alarm_notice", alarm_id=msg_id,
                         alarm_type=p.get("type", "fault"),
                         severity="high" if p.get("critical") else "medium")]
        if kind == "DISPENSE_RESULT":
            status = p.get("status")
            if status == "COMPLETED":
                drawer = self.app.inventory_snapshot()[p["item"]][0]
                return [dict(base, type="dispensing", slot=self.slot_of(drawer), medication=p["item"],
                             quantity=p["qty"], adc_user_id=self._user_of(p["tx"]))]
            if status == "UNCERTAIN":
                # Stock was not moved but medication may have left the drawer: a pharmacist must count.
                return [dict(base, type="alarm_notice", alarm_id=msg_id,
                             alarm_type="dispense UNCERTAIN " + p.get("tx", ""), severity="medium")]
            return []                       # DENIED: local outcome only
        return []                           # AUTH_FAILED and anything else: local only

    # --------------------------------------------------------------- send
    def _send(self, fields: dict) -> dict:
        signed = dict(fields)
        signed["auth"] = sign(self.key, fields)
        ans = self.transport.post(json.dumps(signed).encode())
        for out in ans.get("outbox", []):
            self._handle_outbound(out)
        return ans

    def _handle_outbound(self, m: dict) -> None:
        m = dict(m)
        auth = m.pop("auth", "")
        if not isinstance(auth, str) or not hmac.compare_digest(sign(self.key, m), auth):
            self._say(f"IGNORED outbound {m.get('type')}: bad authentication code")
            return
        t = m.get("type")
        if t == "configuration":
            res = self._to_part_a("CONFIG", {"config_id": m["config_id"], "slot": m["slot"],
                                             "threshold": m["threshold"]})
            self._say(f"configuration slot {m['slot']} threshold {m['threshold']}: ADC says {res}")
            if res.startswith("Accepted"):
                self.thresholds[m["slot"]] = m["threshold"]
                self._pending_own.append(self._own_fields("config_ack", config_id=m["config_id"],
                                                          slot=m["slot"], threshold=m["threshold"]))
        elif t == "inventory_request":
            self._say("Monitoring System asked for inventory")
            self._pending_own.append(self._inventory_fields())
        elif t == "request_reply":
            res = self._to_part_a("REQUEST_REPLY", {k: m.get(k) for k in ("request_id", "decision", "reason", "slot")})
            self._say(f"request_reply {m.get('request_id')}: ADC says {res}")

    # --------------------------------------------------------------- pump
    def pump(self) -> str:
        """One cycle. Returns 'up', 'down', 'cut' or 'held' (a rejection stopped the queue)."""
        with self.lock:
            self._pending_own: List[dict] = []
            pending = self.app.pending_wire()
            for msg_id, wire in pending:                 # translate on first sight
                if msg_id not in self._cache:
                    self._cache[msg_id] = self._translate(msg_id, wire)
            if self.cut:
                return "cut"
            try:
                result = self._drain(pending)
                if result == "held":
                    return "held"
                if self._was_down:                       # reconnect: fresh status and inventory
                    self._pending_own.append(self._own_fields("status", mode=self._mode or "Normal"))
                    self._pending_own.append(self._inventory_fields())
                    self._was_down = False
                    self._last_status = self.now()
                elif self._mode and (self._last_status is None
                                     or self.now() - self._last_status >= self.status_interval_s):
                    self._pending_own.append(self._own_fields("status", mode=self._mode))
                    self._last_status = self.now()
                self._pending_own.extend(self._alarm_clears())
                self._poll_outbox()
                while self._pending_own:
                    f = self._pending_own.pop(0)
                    ans = self._send(f)
                    if ans.get("result") != "accepted":
                        self.rejected.append(f)
                        self._say(f"REJECTED by Monitoring System: {f['type']} #{f['message_id']}")
                # Liveness for Part A's link state. An ACK naming no message lets a
                # RESYNC with an empty queue finish (LinkStateMachine.note_queue_drained).
                self._to_part_a("ACK", {"ack_id": ""})
                return "up"
            except LinkDown as e:
                if not self._was_down:
                    self._say(f"Monitoring System unreachable ({e}); messages stay in the ADC queue")
                self._was_down = True
                return "down"

    def _drain(self, pending: List[Tuple[str, str]]) -> str:
        for msg_id, _wire in pending:
            for f in self._cache[msg_id]:
                if f["message_id"] in self._accepted:
                    continue
                ans = self._send(f)
                if ans.get("result") != "accepted":
                    self.rejected.append(f)
                    self._say(f"REJECTED by Monitoring System: {f['type']} #{f['message_id']} "
                              f"(Part A {msg_id}); queue held")
                    return "held"
                self._accepted.add(f["message_id"])
            res = self._to_part_a("ACK", {"ack_id": msg_id})
            self._cache.pop(msg_id, None)
            self._say(f"{msg_id} delivered: {res}")
        return "ok"

    def _alarm_clears(self) -> List[dict]:
        out = []
        usable = {d for d, state, _lk in self.app.drawer_report() if state == "IN_SERVICE"}
        for drawer in list(self._alarms):
            if drawer in usable:
                for aid in self._alarms.pop(drawer):
                    out.append(self._own_fields("alarm_clear", alarm_id=aid))
        return out

    def _poll_outbox(self) -> None:
        ts = f"{self.now():.3f}"
        auth = hmac.new(self.key.encode(), f"outbox|{self.adc_id}|{ts}".encode(), hashlib.sha256).hexdigest()
        for m in self.transport.get_outbox(self.adc_id, ts, auth).get("messages", []):
            self._handle_outbound(m)

    # ---------------------------------------------------------- threading
    def run_forever(self, period_s: float = 0.5, stop: Optional[threading.Event] = None) -> None:
        stop = stop or threading.Event()
        while not stop.wait(period_s):
            try:
                self.pump()
            except Exception as e:          # keep the ADC running whatever the link does
                self._say(f"adapter error {e!r}")
