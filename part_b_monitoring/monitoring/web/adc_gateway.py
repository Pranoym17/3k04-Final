"""ADC Transport Adapter module.

Responsibility: carry the bytes of the ADC interface between the network and
ADC Communication Manager.
Secret: the transport. Today it is HTTP: the ADC POSTs a message and gets the
answer (including anything waiting for it) in the response, and it GETs its
outbox between messages. Replacing HTTP with a message queue or a socket
changes this file only; ADC Communication Manager takes bytes and returns bytes.

TLS (NFR-SEC-04) is switched on in run_monitoring.py with --tls, not here, so
this module never sees a certificate.

D1 traceability: NFR-SEC-04 (transport side), NFR-PRF-03.
"""
from __future__ import annotations

import json

from flask import Blueprint, Response, request


def create_adc_blueprint(system) -> Blueprint:
    """Build the HTTP transport routes for ADC messages and signed polls."""
    bp = Blueprint("adc_gateway", __name__)

    @bp.post("/adc/messages")
    def inbound():
        answer = system.handle_adc_bytes(request.get_data())
        status = 400 if json.loads(answer).get("result") == "rejected" else 200
        return Response(answer, status=status, mimetype="application/json")

    @bp.get("/adc/outbox")
    def outbox():
        answer = system.poll_outbox(request.args.get("adc_id", ""), request.args.get("ts", ""),
                                    request.args.get("auth", ""))
        status = 403 if "error" in json.loads(answer) else 200
        return Response(answer, status=status, mimetype="application/json")

    return bp
