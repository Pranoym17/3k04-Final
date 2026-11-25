#!/usr/bin/env python3
"""Start the Monitoring System (Operator Dashboard + ADC interface).

    python run_monitoring.py --seed --demo

Options
  --db PATH         history file (default data/history.db)
  --port N          port (default 5000)
  --seed            create demo data when none exists; refresh demo passwords
  --demo            enable the demonstration-only "make storage fail" button
  --time-scale N    divide operational deadlines by N for rehearsal (default 1)
  --tls             serve HTTPS with a self-signed certificate (default, NFR-SEC-04)
  --fresh           delete the history file first
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitoring.policy import Policy                      # noqa: E402
from monitoring.seed import seed                          # noqa: E402
from monitoring.system import MonitoringSystem            # noqa: E402
from monitoring.web.adc_gateway import create_adc_blueprint   # noqa: E402
from monitoring.web.dashboard import create_app           # noqa: E402


def main():
    """Parse launch options and run the Monitoring System."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join("data", "history.db"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--time-scale", type=float, default=1.0)
    ap.add_argument("--tls", action="store_true", default=True)
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)
    if a.fresh:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(a.db + suffix):
                os.remove(a.db + suffix)
    policy = Policy().scaled(a.time_scale) if a.time_scale != 1.0 else Policy()
    system = MonitoringSystem(a.db, policy=policy, demo=a.demo)
    system.start()
    if a.seed:
        made = seed(system)
        if made:
            print("Seeded:", ", ".join(made))
    system.run_ticker()
    app = create_app(system)                                  # M16: pages for staff
    app.config["SESSION_COOKIE_SECURE"] = a.tls
    app.register_blueprint(create_adc_blueprint(system))      # M15: the ADC interface

    ssl_context = None
    if a.tls:
        d = os.path.dirname(os.path.abspath(a.db))
        cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        if not os.path.exists(cert) or not os.path.exists(key):
            openssl = shutil.which("openssl")
            if openssl is None:
                git = shutil.which("git")
                candidate = os.path.join(os.path.dirname(os.path.dirname(git)), "usr", "bin", "openssl.exe") if git else ""
                openssl = candidate if os.path.isfile(candidate) else None
            if openssl is None:
                ap.error("HTTPS needs OpenSSL on PATH (or Git for Windows)")
            subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                            "-out", cert, "-days", "30", "-subj", "/CN=localhost"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ssl_context = (cert, key)
    scheme = "https" if a.tls else "http"
    print(f"Monitoring System on {scheme}://{a.host}:{a.port}  (mode {system.mode.mode().value}, "
          f"time scale {a.time_scale:g})")
    if a.seed:
        print("Accounts: admin1, pharm1, nurse1, tech1, tech2 (password: 12345)")
    app.run(host=a.host, port=a.port, threaded=True, use_reloader=False, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
