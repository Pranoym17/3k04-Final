"""Part A stand-in: simulated ADCs for the Monitoring System demonstration.

This is NOT part of the Monitoring System. It plays the other side of the ADC
interface (assumption A2) so every scenario can be triggered on request:
status every 10 s, inventory reports, alarm notices and clears, dispensing
events, requests, configuration acknowledgements, a silent link with buffering
and replay (A11), and deliberately bad messages.

It talks to the Monitoring System over HTTP or HTTPS, through the same two
endpoints a real ADC would use, and implements its own copy of the signing
rule, so it can catch a mismatch between the interface document and the code.
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
from collections import deque
from typing import Deque, Dict, List, Optional


def sign(key: str, fields: dict) -> str:
    """Compute the independent simulator HMAC over canonical JSON fields."""
    canon = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(key.encode(), canon.encode(), hashlib.sha256).hexdigest()


class SimAdc:
    """Simulate one authenticated cabinet and its demonstration controls."""
    def __init__(self, adc_id: str, key: str, base_url: str, interval: float = 10.0,
                 insecure: bool = False, slots=None, inventory_every: float = 60.0):
        """Initialise simulated cabinet state, HTTP transport and worker controls."""
        self.adc_id, self.key, self.base = adc_id, key, base_url.rstrip("/")
        self.interval = interval
        self.inventory_every = inventory_every
        self.mode = "Normal"
        self.slots: Dict[int, List] = {n: [m, q] for n, m, q in (slots or [
            (1, "Morphine 10mg", 20), (2, "Ondansetron 4mg", 18), (3, "Heparin 5000u", 12),
            (4, "Saline 0.9%", 30)])}
        self.link_up = True
        self.auto_ack = True
        self.mid = 0
        self.alarm_n = 0
        self.last_alarm: Optional[str] = None
        self.last_raw: Optional[bytes] = None
        self.buffer: List[dict] = []
        self.log: Deque[str] = deque(maxlen=60)
        self._lock = threading.RLock()
        self._ctx = ssl._create_unverified_context() if insecure else None
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    # ----------------------------------------------------------- lifecycle
    def start(self):
        """Start status, inventory and outbox polling workers."""
        for target in (self._status_loop, self._poll_loop, self._inventory_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        """Signal all simulator workers to stop."""
        self._stop.set()

    def _say(self, text: str):
        self.log.appendleft(f"{time.strftime('%H:%M:%S')}  {text}")

    # ------------------------------------------------------------ messages
    def build(self, mtype: str, **fields) -> dict:
        """Build a cabinet message with the next ID and current ADC time."""
        with self._lock:
            self.mid += 1
            f = {"adc_id": self.adc_id, "message_id": self.mid, "timestamp": time.time(), "type": mtype}
            f.update(fields)
            return f

    def deliver(self, f: dict, bad_auth: bool = False, key: Optional[str] = None) -> Optional[dict]:
        """Send now, or buffer when the link is down (A11)."""
        with self._lock:
            if not self.link_up:
                if len(self.buffer) < 500:
                    self.buffer.append(f)
                self._say(f"link down: buffered {f['type']} #{f['message_id']}")
                return None
            return self._post(f, bad_auth, key)

    def _post(self, f: dict, bad_auth: bool = False, key: Optional[str] = None) -> Optional[dict]:
        g = dict(f)
        g["auth"] = "0" * 64 if bad_auth else sign(key or self.key, f)
        raw = json.dumps(g).encode()
        self.last_raw = raw
        return self._post_raw(raw, f"{f['type']} #{f['message_id']}")

    def _post_raw(self, raw: bytes, label: str) -> Optional[dict]:
        req = urllib.request.Request(self.base + "/adc/messages", data=raw,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5, context=self._ctx) as r:
                ans = json.loads(r.read())
        except urllib.error.HTTPError as e:
            ans = json.loads(e.read() or b"{}")
        except Exception as e:  # server not reachable
            self._say(f"{label}: cannot reach Monitoring System ({e.__class__.__name__})")
            return None
        self._say(f"{label}: {ans.get('result')} {ans.get('reason') or ''}".rstrip())
        for m in ans.get("outbox", []):
            self._handle_outbound(m)
        return ans

    # ------------------------------------------------------ outbound handling
    def _handle_outbound(self, m: dict):
        auth = m.pop("auth", "")
        if not hmac.compare_digest(sign(self.key, m), auth):
            self._say(f"IGNORED outbound {m.get('type')}: bad authentication code")
            return
        t = m.get("type")
        if t == "inventory_request":
            self._say("Monitoring System asks for inventory")
            self.send_inventory()
        elif t == "configuration":
            self._say(f"configuration: slot {m['slot']} threshold {m['threshold']} (id {m['config_id']})")
            if self.auto_ack:
                self.deliver(self.build("config_ack", config_id=m["config_id"], slot=m["slot"],
                                        threshold=m["threshold"]))
            else:
                self._say("auto-acknowledge is off: not acknowledging")
        elif t == "request_reply":
            slot = f" slot {m['slot']}" if m.get("slot") is not None else ""
            self._say(f"REPLY to {m['request_id']}{slot}: {m['decision'].upper()} ({m['reason']})")

    # ------------------------------------------------------------- loops
    def _status_loop(self):
        while not self._stop.wait(self.interval):
            self.send_status()

    def _inventory_loop(self):
        while not self._stop.wait(self.inventory_every):
            self.send_inventory()

    def _poll_loop(self):
        while not self._stop.wait(2.0):
            if not self.link_up:
                continue
            ts = f"{time.time():.3f}"
            auth = hmac.new(self.key.encode(), f"outbox|{self.adc_id}|{ts}".encode(), hashlib.sha256).hexdigest()
            url = f"{self.base}/adc/outbox?" + urllib.parse.urlencode(
                {"adc_id": self.adc_id, "ts": ts, "auth": auth})
            try:
                with urllib.request.urlopen(url, timeout=5, context=self._ctx) as r:
                    for m in json.loads(r.read()).get("messages", []):
                        self._handle_outbound(m)
            except Exception:
                pass

    # ------------------------------------------------------------ actions
    def send_status(self):
        """Send the current simulated ADC mode."""
        return self.deliver(self.build("status", mode=self.mode))

    def send_inventory(self):
        """Send a complete report of simulated medication slots."""
        slots = [{"slot": n, "medication": m, "quantity": q} for n, (m, q) in sorted(self.slots.items())]
        return self.deliver(self.build("inventory", slots=slots))

    def raise_alarm(self, alarm_type="door open", severity="high"):
        """Create and send a simulated ADC alarm notice."""
        with self._lock:
            self.alarm_n += 1
            self.last_alarm = f"A-{self.alarm_n:03d}"
        self._say(f"raising alarm {self.last_alarm} ({alarm_type}, {severity})")
        return self.deliver(self.build("alarm_notice", alarm_id=self.last_alarm,
                                       alarm_type=alarm_type, severity=severity))

    def clear_alarm(self, alarm_id: Optional[str] = None):
        """Clear the named or most recent simulated ADC alarm."""
        aid = alarm_id or self.last_alarm
        if not aid:
            self._say("no alarm to clear")
            return None
        return self.deliver(self.build("alarm_clear", alarm_id=aid))

    def set_mode(self, mode: str):
        """Set the simulated cabinet mode and report it."""
        self.mode = mode
        return self.send_status()

    def set_quantity(self, slot: int, qty: int):
        """Set one simulated slot quantity and send an inventory report."""
        if slot in self.slots:
            self.slots[slot][1] = qty
        return self.send_inventory()

    def dispense(self, slot: int, qty: int, user: str):
        """Apply and report a dispensing event without patient data."""
        if slot in self.slots:
            self.slots[slot][1] = max(0, self.slots[slot][1] - qty)
        r = self.deliver(self.build("dispensing", slot=slot, medication=self.slots.get(slot, ["unknown"])[0],
                                    quantity=qty, adc_user_id=user))
        self.send_inventory()
        return r

    def request_refill(self, slots: List[int]):
        """Send a refill request for the supplied slots."""
        with self._lock:
            rid = f"R-{int(time.time()) % 100000}-{self.mid + 1}"
        return self.deliver(self.build("request", request_id=rid, request_type="refill", slots=slots))

    def request_maintenance(self):
        """Send a maintenance request with a new request ID."""
        with self._lock:
            rid = f"M-{int(time.time()) % 100000}-{self.mid + 1}"
        return self.deliver(self.build("request", request_id=rid, request_type="maintenance"))

    def silence(self):
        """Simulate link loss while buffering attempted messages."""
        self.link_up = False
        self._say("LINK DOWN: the ADC keeps running and buffers its messages")

    def resume(self):
        """Restore the link, replay buffered messages and report current state."""
        with self._lock:
            self.link_up = True
            pending, self.buffer = self.buffer, []
        self._say(f"LINK UP: replaying {len(pending)} buffered message(s), oldest first")
        for f in pending:
            self._post(f)
        self.send_status()

    # ---- deliberately bad messages (off-normal scenarios)
    def bad_auth(self):
        """Send a status message with an intentionally wrong authentication code."""
        return self._post(self.build("status", mode=self.mode), bad_auth=True)

    def duplicate(self):
        """Resend the previous raw message unchanged."""
        if self.last_raw:
            return self._post_raw(self.last_raw, "duplicate of the last message")
        return None

    def malformed(self):
        """Send an alarm notice without its required alarm ID."""
        f = self.build("alarm_notice", alarm_type="door open", severity="high")   # no alarm_id
        return self._post(f)

    def unregistered(self):
        """Send a status claiming an unregistered ADC ID."""
        f = {"adc_id": "ADC-99", "message_id": 1, "timestamp": time.time(), "type": "status", "mode": "Normal"}
        return self._post(f, key="some-other-key")

    def patient_field(self):
        """Send an intentionally invalid message containing a patient field."""
        f = self.build("dispensing", slot=1, medication="Morphine 10mg", quantity=1, adc_user_id="RN-7",
                       patient_id="P-1234")
        return self._post(f)
