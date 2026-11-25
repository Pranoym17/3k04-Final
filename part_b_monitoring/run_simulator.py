#!/usr/bin/env python3
"""Start the ADC stand-in (Part A simulator) and its control page.

    python run_simulator.py

Options
  --url URL         Monitoring System address (default https://127.0.0.1:5000)
  --adcs A,B,C      ADC IDs to simulate (default ADC-01,ADC-02,ADC-03)
  --time-scale N    send status every 10/N seconds (use the same N as the server)
  --insecure        accept the self-signed certificate of --tls
  --port N          control page port (default 5001)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adc_simulator.control import create_control_app      # noqa: E402
from adc_simulator.sim import SimAdc                      # noqa: E402


def main():
    """Parse launch options and run the ADC stand-in."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="https://127.0.0.1:5000")
    ap.add_argument("--adcs", default="ADC-01,ADC-02,ADC-03")
    ap.add_argument("--time-scale", type=float, default=1.0)
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--port", type=int, default=5001)
    a = ap.parse_args()
    adcs = {}
    for adc_id in [x.strip() for x in a.adcs.split(",") if x.strip()]:
        s = SimAdc(adc_id, f"demo-key-{adc_id}", a.url, interval=10.0 / a.time_scale,
                   inventory_every=60.0 / a.time_scale, insecure=a.insecure)
        s.start()
        adcs[adc_id] = s
    print(f"ADC stand-in control page: http://127.0.0.1:{a.port}")
    create_control_app(adcs, a.url).run(host="127.0.0.1", port=a.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
