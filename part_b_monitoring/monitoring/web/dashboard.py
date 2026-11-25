"""Operator Dashboard module.

Responsibility: everything a person sees and clicks: login, the dashboard of
cabinets and alarms, inventory and threshold pages, history, reports, requests
and administration. It contains no rules. Every decision (who may do what,
what Stale means, whether a change is allowed) is asked of the module that owns
it, and every refusal is shown with its reason.
Secret: layout, wording, colours, page structure and the web framework. A
translated or replaced screen changes this folder only.

Controls follow Access Control permissions; the server independently checks
and records attempts, including requests sent without a visible control.

D1 traceability: FR-DSH-01, FR-INV-01 (display), FR-ALM-01/-05 (display),
FR-HIS-02/-04/-06 (display), FR-REQ-04 (display), FR-MOD-03 (banner),
NFR-SAF-01, NFR-USA-01, NFR-USA-02, NFR-PRF-04.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from functools import wraps
from typing import Optional, Tuple

from flask import (Flask, Response, abort, flash, make_response, redirect,
                   render_template, request, url_for)

from ..domain import Category, Fn

COOKIE = "ms_session"
_HERE = os.path.dirname(os.path.abspath(__file__))


def fmt_time(ts) -> str:
    """Format epoch seconds for display, using a dash for a missing time."""
    if ts is None or ts == "":
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError, TypeError):
        return str(ts)


def parse_range(args, now: float) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Read `start` and `end` (datetime-local text) from a form or query.
    Returns (start, end, error). Empty fields mean the last 24 hours before
    `now`, which the caller takes from the Clock module."""
    def one(name):
        v = (args.get(name) or "").strip()
        if not v:
            return None
        return datetime.fromisoformat(v).timestamp()
    try:
        start, end = one("start"), one("end")
    except ValueError:
        return None, None, "Dates must look like 2026-11-03T14:30."
    end = now if end is None else end
    start = end - 86400 if start is None else start
    if start > end:
        return None, None, "The start of the range is after its end. Choose a valid range."
    return start, end, None


def create_app(system) -> Flask:
    """Build the Flask dashboard with server-side session and role guards."""
    app = Flask(__name__, template_folder=os.path.join(_HERE, "templates"),
                static_folder=os.path.join(_HERE, "static"))
    app.secret_key = secrets.token_hex(16)
    app.config["ADC_SIMULATOR_URL"] = os.environ.get("ADC_SIMULATOR_URL", "http://127.0.0.1:5001")
    app.jinja_env.filters["t"] = fmt_time

    # ------------------------------------------------------------ helpers
    def session_info():
        with system.lock:
            return system.access.session(request.cookies.get(COOKIE), touch=False)

    @app.context_processor
    def layout():
        with system.lock:
            info = session_info()
            return {"me": info, "banner": system.mode.banner(), "sys_mode": system.mode.mode().value,
                    "Fn": Fn, "demo": system.demo, "demo_policy": system.policy,
                    "permits": (lambda fn: bool(info) and system.access.permits(info.role, fn))}

    def guarded(fn: Fn):
        """Every page passes through here: no session -> login (FR-AUT-01); a
        session whose role is not allowed -> 403, refusal recorded (FR-AUT-02)."""
        def deco(view):
            @wraps(view)
            def wrapper(*a, **kw):
                with system.lock:
                    auth = system.access.authorise(request.cookies.get(COOKIE), fn,
                                                   touch=request.endpoint != "dashboard_live")
                    if auth is None:
                        if session_info() is None:
                            return redirect(url_for("login", next=request.path))
                        return render_template("denied.html", what=fn.value), 403
                    return view(auth, *a, **kw)
            return wrapper
        return deco

    def back(endpoint, **kw):
        return redirect(url_for(endpoint, **kw))

    def result_flash(res, ok_text):
        flash((ok_text if res.ok else f"Refused: {res.reason}"), "ok" if res.ok else "error")

    def _refused_input(auth, reason):
        system.history.append(Category.ACCESS, "action_refused",
                              {"function": auth.fn.value, "reason": reason}, user_id=auth.user_id)
        flash(f"Refused: {reason}", "error")

    # -------------------------------------------------------------- login
    @app.route("/")
    def index():
        return back("dashboard") if session_info() else back("login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            with system.lock:
                if "demo_account" in request.form:
                    res = system._demo_login(request.form.get("demo_account", ""))
                else:
                    res = system.access.login(request.form.get("user_id", "").strip(),
                                              request.form.get("password", ""))
            if res.ok:
                nxt = request.args.get("next") or url_for("dashboard")
                if nxt == url_for("dashboard_live"):
                    nxt = url_for("dashboard")
                local = nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt
                resp = make_response(redirect(nxt if local else url_for("dashboard")))
                resp.set_cookie(COOKIE, res.value, httponly=True, samesite="Strict", secure=request.is_secure)
                return resp
            flash(res.reason, "error")
        with system.lock:
            return render_template("login.html", demo_accounts=system._demo_accounts())

    @app.post("/logout")
    def logout():
        with system.lock:
            system.access.logout(request.cookies.get(COOKIE) or "")
        resp = make_response(redirect(url_for("login")))
        resp.delete_cookie(COOKIE)
        return resp

    # ---------------------------------------------------------- dashboard
    def live_context(auth):
        rows = []
        for adc in system.config.registered_ids():
            snap = system.health.snapshot(adc)
            rows.append({"adc_id": adc, "state": snap.state.value, "stale": snap.stale,
                         "last": snap.last_valid_receive, "mode": snap.adc_mode.value if snap.adc_mode else "unknown",
                         "alarms": system.alarms.active_count(adc), "low": system.inventory.low_count(adc)})
        pending = (system.requests.pending_maintenance()
                   if system.access.permits(auth.role, Fn.DECIDE_MAINTENANCE) else [])
        return {"rows": rows, "alarms": system.alarms.active_alarms(),
                "escalated": system.alarms.has_escalated()
                and system.access.permits(auth.role, Fn.ACK_ALARM),
                "pending": pending, "auth": auth}

    @app.get("/dashboard")
    @guarded(Fn.VIEW_DASHBOARD)
    def dashboard(auth):
        return render_template("dashboard.html", **live_context(auth))

    @app.get("/dashboard/live")
    @guarded(Fn.VIEW_DASHBOARD)
    def dashboard_live(auth):
        response = make_response(render_template("_live.html", **live_context(auth)))
        response.headers["X-Monitoring-Mode"] = system.mode.mode().value
        response.headers["X-Monitoring-Banner"] = system.mode.banner() or ""
        return response

    @app.post("/alarms/ack")
    @guarded(Fn.ACK_ALARM)
    def ack_alarm(auth):
        res = system.alarms.acknowledge(auth, request.form.get("key", ""))
        result_flash(res, "Alarm acknowledged.")
        return back("dashboard")

    @app.post("/requests/decide")
    @guarded(Fn.DECIDE_MAINTENANCE)
    def decide(auth):
        if request.form.get("decision") not in ("accept", "reject"):
            _refused_input(auth, "decision must be accept or reject")
            return back("dashboard")
        accept = request.form.get("decision") == "accept"
        res = system.requests.decide_maintenance(auth, request.form.get("key", ""), accept)
        result_flash(res, "Decision sent to the ADC.")
        return back("dashboard")

    # ---------------------------------------------------------- inventory
    @app.get("/adc/<adc_id>")
    @guarded(Fn.VIEW_DASHBOARD)
    def adc_page(auth, adc_id):
        if not system.config.is_registered(adc_id):
            abort(404)
        slots = system.inventory.slots(adc_id)
        latest = {v.slot: system.config.latest_change(adc_id, v.slot) for v in slots}
        return render_template("adc.html", adc_id=adc_id, snap=system.health.snapshot(adc_id),
                               slots=slots, latest=latest, report=system.inventory.last_report(adc_id))

    @app.post("/adc/<adc_id>/threshold")
    @guarded(Fn.CHANGE_THRESHOLD)
    def threshold(auth, adc_id):
        try:
            slot, value = int(request.form.get("slot", "")), int(request.form.get("value", ""))
        except ValueError:
            system.history.append(Category.ACCESS, "action_refused",
                                  {"function": Fn.CHANGE_THRESHOLD.value,
                                   "reason": "slot and value must be whole numbers"}, user_id=auth.user_id)
            flash("Refused: slot and value must be whole numbers.", "error")
            return back("adc_page", adc_id=adc_id)
        res = system.config.request_threshold_change(auth, adc_id, slot, value)
        result_flash(res, "Change sent. It shows Unconfirmed until the ADC acknowledges it.")
        return back("adc_page", adc_id=adc_id)

    # ------------------------------------------------------------ history
    @app.get("/history")
    @guarded(Fn.VIEW_HISTORY)
    def history(auth):
        adc_id = request.args.get("adc_id", "")
        view = request.args.get("view", "events")
        events, report, error = None, None, None
        if adc_id:
            start, end, error = parse_range(request.args, system.clock.now())
            if error:
                _refused_input(auth, error)
            if not error:
                if view == "dispensing":
                    report = system.reports.dispensing_list(auth, adc_id, start, end).value
                else:
                    events = system.history.query(adc_id=adc_id, start=start, end=end)
        return render_template("history.html", adc_ids=_known_adcs(), adc_id=adc_id, view=view,
                               events=events, report=report, error=error, args=request.args)

    def _known_adcs():
        seen = set(system.config.registered_ids())
        for e in system.history.query(kind="adc_registered"):
            seen.add(e.adc_id)
        return sorted(seen)

    # ------------------------------------------------------------ reports
    @app.get("/reports")
    @guarded(Fn.VIEW_DASHBOARD)
    def reports_index(auth):
        return render_template("reports.html", adc_ids=_known_adcs())

    def show_report(res, error_endpoint):
        if not res.ok:
            flash(f"Refused: {res.reason}", "error")
            return back(error_endpoint)
        if request.args.get("format") == "csv":
            return Response(system.reports.to_csv(res.value), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=report.csv"})
        return render_template("report_view.html", report=res.value, args=request.args)

    @app.get("/reports/inventory")
    @guarded(Fn.RUN_INVENTORY_REPORT)
    def report_inventory(auth):
        return show_report(system.reports.inventory_report(auth), "reports_index")

    @app.get("/reports/alarms")
    @guarded(Fn.RUN_ALARM_AVAIL_REPORTS)
    def report_alarms(auth):
        start, end, error = parse_range(request.args, system.clock.now())
        if error:
            _refused_input(auth, error)
            return back("reports_index")
        return show_report(system.reports.alarm_report(auth, start, end, request.args.get("adc_id") or None),
                           "reports_index")

    @app.get("/reports/availability")
    @guarded(Fn.RUN_ALARM_AVAIL_REPORTS)
    def report_availability(auth):
        start, end, error = parse_range(request.args, system.clock.now())
        if error:
            _refused_input(auth, error)
            return back("reports_index")
        return show_report(system.reports.availability_report(auth, start, end), "reports_index")

    # -------------------------------------------------------------- admin
    @app.get("/admin")
    @guarded(Fn.ADMIN)
    def admin(auth):
        adcs = [{"adc_id": a, "state": system.health.state_of(a).value} for a in system.config.registered_ids()]
        return render_template("admin.html", users=system.access.list_users(), adcs=adcs,
                               roles=system.access._roles(), storage_failing=system.history.failure_injected())

    @app.post("/admin/users/create")
    @guarded(Fn.ADMIN)
    def admin_create_user(auth):
        role = system.access._role_for_name(request.form.get("role", ""))
        res = system.access.create_user(auth, request.form.get("user_id", "").strip(), role,
                                        request.form.get("password", ""))
        result_flash(res, "Account created.")
        return back("admin")

    @app.post("/admin/users/disable")
    @guarded(Fn.ADMIN)
    def admin_disable_user(auth):
        result_flash(system.access.disable_user(auth, request.form.get("user_id", "")), "Account disabled.")
        return back("admin")

    @app.post("/admin/adcs/register")
    @guarded(Fn.ADMIN)
    def admin_register(auth):
        res = system.config.register_adc(auth, request.form.get("adc_id", "").strip(),
                                         request.form.get("key", "").strip() or None)
        if res.ok:
            flash(f"ADC registered. Its key is {res.value} (shown once; give it to the ADC team).", "ok")
        else:
            flash(f"Refused: {res.reason}", "error")
        return back("admin")

    @app.post("/admin/adcs/deregister")
    @guarded(Fn.ADMIN)
    def admin_deregister(auth):
        result_flash(system.config.deregister_adc(auth, request.form.get("adc_id", "")), "ADC deregistered.")
        return back("admin")

    @app.post("/admin/mode")
    @guarded(Fn.ADMIN)
    def admin_mode(auth):
        target = request.form.get("mode")
        if target not in ("maintenance", "normal"):
            _refused_input(auth, "mode must be maintenance or normal")
            return back("admin")
        res = (system.mode.switch_to_maintenance(auth) if target == "maintenance"
               else system.mode.switch_to_normal(auth))
        result_flash(res, f"Mode is now {system.mode.mode().value}.")
        return back("admin")

    @app.get("/admin/audit")
    @guarded(Fn.ADMIN)
    def admin_audit(auth):
        user_id = request.args.get("user_id", "").strip()
        actions, access, error = None, None, None
        if user_id:
            start, end, error = parse_range(request.args, system.clock.now())
            if error:
                _refused_input(auth, error)
            if not error:
                actions = system.history.query(category=Category.USER_ACTION, user_id=user_id,
                                               start=start, end=end)
                access = system.history.query(category=Category.ACCESS, user_id=user_id,
                                              start=start, end=end)
        return render_template("audit.html", users=system.access.list_users(), user_id=user_id,
                               actions=actions, access=access, error=error, args=request.args)

    @app.post("/admin/demo/storage")
    @guarded(Fn.ADMIN)
    def admin_demo_storage(auth):
        if not system.demo:
            abort(404)
        if request.form.get("state") not in ("fail", "ok"):
            _refused_input(auth, "storage state must be fail or ok")
            return back("admin")
        failing = request.form.get("state") == "fail"
        system.history.inject_failure(failing)
        if failing:   # the next write fails at once, so Fault mode starts now
            system.history.append(Category.SYSTEM, "demo_storage_failure", {"note": "failure switched on"})
        flash("Storage failure " + ("switched on." if request.form.get("state") == "fail"
                                    else "switched off. The next test write restores Normal mode."), "ok")
        return back("admin")

    return app
