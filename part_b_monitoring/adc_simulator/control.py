"""Control page of the ADC stand-in (port 5001 by default).

One card per simulated ADC with a button for every scenario the TAs may ask
for. It only calls methods of SimAdc; it never touches the Monitoring System.
"""
from __future__ import annotations

from typing import Dict

from flask import Flask, redirect, render_template_string, request, url_for

from .sim import SimAdc

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ADC stand-in (Part A simulator)</title>
<style>
*{box-sizing:border-box} :root{color-scheme:dark}
body{font:16px/1.55 "Segoe UI",system-ui,sans-serif;margin:0 auto;padding:32px;max-width:1360px;color:#fff;background:#000}
a{color:#c5dcff;text-underline-offset:4px}
.card{border:1px solid #343438;border-radius:18px;background:#111113;padding:24px;margin:24px 0}
h1{font-size:clamp(24px,4vw,36px);font-weight:500;letter-spacing:-.04em} h2{font-size:20px;font-weight:500;margin:0 0 12px}
h3{font-size:13px;font-weight:500;color:#bcbcc4;text-transform:uppercase;letter-spacing:.06em;margin:0 0 12px}
.control-group{padding:20px 0;border-top:1px solid #343438}
form{display:inline-flex;flex-wrap:wrap;align-items:center;gap:8px;margin:4px 8px 4px 0;max-width:100%}
button{font:inherit;font-size:13px;min-height:44px;padding:10px 16px;border:1px solid #55555b;border-radius:999px;background:#28282a;color:#fff;cursor:pointer}
button:hover{background:#fff;color:#111}
input,select{font:inherit;min-height:44px;max-width:100%;width:7rem;padding:8px;border:1px solid #66666f;border-radius:8px;background:#101012;color:#fff}
:focus-visible{outline:3px solid #a7c9ff;outline-offset:4px}
pre{background:#08080a;border:1px solid #343438;border-radius:12px;padding:16px;max-height:14rem;overflow:auto;font-size:13px;color:#c8c8d0}
.off{font-weight:700;color:#ffb0b4;text-decoration:underline}
.hint{color:#aaaab3;font-size:13px}
@media(max-width:720px){body{padding:16px}.card{padding:16px}form{display:flex;margin:10px 0}button{flex-grow:1}h2{font-size:17px}}
</style></head><body>
<h1>ADC stand-in (Part A simulator) <span class="hint">sends to {{ base }}</span></h1>
<p class="hint">Everything here is the other side of the interface. Use the Monitoring System in the other browser tab to watch the effect.
<a href="{{ url_for('index') }}">Refresh</a></p>
{% for a in adcs.values() %}
<div class="card"><h2>{{ a.adc_id }}
 &middot; mode {{ a.mode }} &middot; link <span class="{{ '' if a.link_up else 'off' }}">{{ 'UP' if a.link_up else 'DOWN (buffering ' ~ a.buffer|length ~ ')' }}</span>
 &middot; auto-acknowledge {{ 'on' if a.auto_ack else 'off' }}</h2>
<p>Slots: {% for n, s in a.slots|dictsort %}<strong>{{ n }}</strong> {{ s[0] }} = {{ s[1] }}; {% endfor %}</p>
<div class="control-group"><h3>Connection and recovery</h3>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='status') }}"><button>Send status</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='inventory') }}"><button>Send inventory</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='silence') }}"><button>Silence (link down)</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='resume') }}"><button>Resume (replay)</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='autoack') }}"><button>Toggle auto-acknowledge</button></form>
</div><div class="control-group"><h3>Alarms and cabinet mode</h3>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='alarm') }}">
  <select name="severity" aria-label="Alarm severity"><option>high</option><option>medium</option><option>low</option></select>
  <input name="alarm_type" aria-label="Alarm type" value="door open" size="10"><button>Raise alarm</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='clear') }}"><button>Clear last alarm</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='mode') }}">
  <select name="mode" aria-label="Cabinet mode"><option>Normal</option><option>Fault</option><option>Maintenance</option></select><button>Set mode</button></form>
</div><div class="control-group"><h3>Inventory and dispensing</h3>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='qty') }}">
  slot <input name="slot" aria-label="Inventory slot" type="number" value="3" style="width:3.5rem"> qty <input name="qty" aria-label="Inventory quantity" type="number" value="4" style="width:4rem"><button>Set quantity</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='dispense') }}">
  slot <input name="slot" aria-label="Dispensing slot" type="number" value="1" style="width:3.5rem"> qty <input name="qty" aria-label="Dispensing quantity" type="number" value="1" style="width:3.5rem">
  user <input name="user" aria-label="ADC user ID" value="RN-7" style="width:4.5rem"><button>Dispense</button></form>
</div><div class="control-group"><h3>Refill and maintenance requests</h3>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='refill') }}">
  slots <input name="slots" aria-label="Refill slots, separated by commas" value="3,4" style="width:5rem"><button>Refill request</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='maint') }}"><button>Maintenance request</button></form>
</div><div class="control-group"><h3>Validation drill</h3>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='badauth') }}"><button>Wrong authentication code</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='dup') }}"><button>Send last message again</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='malformed') }}"><button>Alarm with no alarm ID</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='unreg') }}"><button>Message from unregistered ADC-99</button></form>
 <form method="post" action="{{ url_for('act', adc=a.adc_id, what='patient') }}"><button>Message with a patient field</button></form>
</div>
<pre>{{ a.log|join('\\n') }}</pre></div>
{% endfor %}
</body></html>"""


def create_control_app(adcs: Dict[str, SimAdc], base_url: str) -> Flask:
    """Build the stand-in page and action routes for the supplied ADCs."""
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template_string(PAGE, adcs=adcs, base=base_url)

    @app.post("/<adc>/<what>")
    def act(adc, what):
        a = adcs[adc]
        f = request.form
        if what == "status": a.send_status()
        elif what == "inventory": a.send_inventory()
        elif what == "silence": a.silence()
        elif what == "resume": a.resume()
        elif what == "autoack": a.auto_ack = not a.auto_ack
        elif what == "alarm": a.raise_alarm(f.get("alarm_type", "door open"), f.get("severity", "high"))
        elif what == "clear": a.clear_alarm()
        elif what == "mode": a.set_mode(f.get("mode", "Normal"))
        elif what == "qty": a.set_quantity(int(f.get("slot", 1)), int(f.get("qty", 0)))
        elif what == "dispense": a.dispense(int(f.get("slot", 1)), int(f.get("qty", 1)), f.get("user", "RN-7"))
        elif what == "refill":
            a.request_refill([int(x) for x in f.get("slots", "3").split(",") if x.strip()])
        elif what == "maint": a.request_maintenance()
        elif what == "badauth": a.bad_auth()
        elif what == "dup": a.duplicate()
        elif what == "malformed": a.malformed()
        elif what == "unreg": a.unregistered()
        elif what == "patient": a.patient_field()
        return redirect(url_for("index"))

    return app
