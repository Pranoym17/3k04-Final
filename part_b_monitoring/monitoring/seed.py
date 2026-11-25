"""Demonstration seed (not part of the Monitoring System design).

Assumption A10 says user accounts exist before the system is used. For the
demonstration this script creates one account per role and registers three
cabinets whose keys match the ADC stand-in. It builds an Authorisation by hand,
which only this bootstrap is allowed to do; every other path gets its
Authorisation from Access Control.
"""
from __future__ import annotations

from .domain import Authorisation, Fn, Role

ACCOUNTS = [("admin1", Role.ADMINISTRATOR, "12345"),
            ("pharm1", Role.PHARMACIST, "12345"),
            ("nurse1", Role.NURSE, "12345"),
            ("tech1", Role.TECHNICIAN, "12345"),
            ("tech2", Role.TECHNICIAN, "12345")]
DEMO_ADCS = ["ADC-01", "ADC-02", "ADC-03"]


def demo_key(adc_id: str) -> str:
    """Return the documented demonstration key for an ADC ID."""
    return f"demo-key-{adc_id}"


def seed(system, with_adcs: bool = True) -> list:
    """Seed demonstration accounts and ADCs while holding the system lock."""
    made = []
    with system.lock:
        empty = not system.access.has_users()
        existing = {u["user_id"] for u in system.access.list_users()}
        boot = Authorisation("admin1", Role.ADMINISTRATOR, Fn.ADMIN)
        changed = False
        for uid, role, pw in ACCOUNTS:
            if empty or uid in existing:
                result = system.access._seed_demo_account(boot, uid, role, pw)
                changed = changed or (result.ok and bool(result.value))
        if changed:
            made.append("accounts" if empty else "demo passwords")
        if with_adcs and not system.config.registered_ids():
            boot = Authorisation("admin1", Role.ADMINISTRATOR, Fn.ADMIN)
            for adc in DEMO_ADCS:
                system.config.register_adc(boot, adc, demo_key(adc))
            made.append("ADCs")
    return made
