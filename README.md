# 3K04 Final: ADC and Monitoring System, integrated (Deliverable 3)

| Folder | What | From |
|---|---|---|
| `part_a_adc/` | Part A, the Automated Dispensing Cabinet. Console with simulated hardware. Python 3.9+, standard library only. | `Nijjea1/Software3K04-D2` |
| `part_b_monitoring/` | Part B, the Monitoring System. Flask dashboard and signed HTTPS ADC interface, append-only SQLite history. | `Pranoym17/3k04` (sub team 29) |
| `integration/` | The Monitoring Link Adapter that connects them, launchers, cross part tests, and the integration documents. | Deliverable 3 |

Requirements: Python 3.9 or later, `pip install flask`. HTTPS certificate generation uses OpenSSL on PATH or
the copy in Git for Windows. `pip install coverage` only to measure coverage.

## Run both together

    python integration/run_demo.py               # Monitoring on https://127.0.0.1:5000 + ADC console here
    python integration/run_demo.py --scripted    # automatic rehearsal of both demo scenarios

Dashboard login `admin1` / `12345` (also `pharm1`, `nurse1`, `tech1`). ADC console cards `card-nurse`,
`card-pharm`, `card-tech`. The demo script is in `integration/DEMO.md`.

## Run each part alone

    cd part_a_adc && python -m adc.main
    cd part_b_monitoring && python run_monitoring.py --fresh --seed --demo
    cd part_b_monitoring && python run_simulator.py --insecure      # Part B's own ADC stand-in

## Tests

    cd part_a_adc && python -m unittest discover -s tests -t .                     # 27 tests
    cd part_b_monitoring && python -W ignore -m unittest discover -s tests         # 374 tests, about 2.5 min
    python -W ignore -m unittest discover -s integration/tests                     # 17 cross part tests, from the root

## Interface

`integration/INTERFACE.md` is the contract. In short: the ADC posts signed JSON (HMAC-SHA256 under a key per
ADC) to `POST /adc/messages` and polls `GET /adc/outbox`; types status, inventory, dispensing, alarm_notice,
alarm_clear, config_ack go up; configuration, inventory_request, request_reply come down. The adapter
(`integration/adc_bridge.py`) translates Part A's `{"v","id","kind","payload"}` queue to that format. Part B is
unchanged; Part A has one added read-only method (`integration/CHANGES.md`).
