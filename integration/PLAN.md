# Integration plan (Deliverable 3)

Written 25 November 2026 after read-only discovery. Status at the end of each item in brackets.

## Inventory

| | Part A, ADC | Part B, Monitoring System |
|---|---|---|
| Source | `Nijjea1/Software3K04-D2` at `760b846` | `Pranoym17/3k04` at `8d9b684` (+ README edit) |
| Here | `part_a_adc/` | `part_b_monitoring/` |
| Language | Python 3.9+, standard library only | Python 3.9+, Flask |
| Size | 2,467 lines incl. tests | 3,946 lines of code, 19 test files |
| Run | `python -m adc.main` (console, simulated hardware) | `python run_monitoring.py --fresh --seed --demo` (HTTPS dashboard) |
| Test | `python -m unittest discover -s tests -t .` | `python -W ignore -m unittest discover -s tests` |
| Tests at import | 21 OK | 374 OK |
| Git authors | Avneet Singh Nijjer (inferred from git history, confirm) | Pranoy Mukherjee |

## Part A modules and USES (from file docstrings, confirmed from imports)

Hardware hiding 1.1: `clock` (1.1.5), `credential_reader`, `drawer_sensor`, `lock_driver`, `removal_sensor`.
Behaviour hiding 1.2: `authorization`, `dispense_transaction` (1.2.3), `drawer_state`, `link_state` (1.2.6),
`mode_manager`, `session_manager`, `timing_policy` (1.2.7). Software decisions 1.3: `inventory_store` (1.3.1),
`audit_log_store`, `outbound_queue` (1.3.3), `message_codec` (1.3.4), `ui_presentation`.
`app.py` (Module 1) USES all of them; `session_manager` USES credential_reader, clock, timing_policy, authorization;
`link_state` USES clock, timing_policy; `audit_log_store` USES clock. No module below `app.py` imports `app.py`,
so the relation is acyclic. `main.py` is the console and USES `app.py` and the simulated hardware.

Safety critical: lock driver and `app.confirm_dispense` (which drawer unlocks, INV-G3), `dispense_transaction`
plus `inventory_store` (when stock moves, R1..R6, INV-I2), `mode_manager` (no unlock outside NORMAL, INV-G1),
`app.poll_hardware` (lock and drawer faults), `session_manager` (who is dispensing), `outbound_queue` and
`link_state` (what Monitoring learns), `clock`.

## Approach and order

Part B is the receiver and stays unchanged. Part A conforms through a thin adapter on the ADC side
(`integration/adc_bridge.py`). Order, each step with a cross part test before the next:
1. status and first contact [done, X00]
2. inventory [done, X00, X02]
3. alarm_notice and alarm_clear [done, X01, X03]
4. dispensing [done, X02, X09]
5. request and reply [not integrated: Part A has no request function]
6. configuration and config_ack [done, partial: X04, X11]
7. off normal: link drop (X05, X12), replay Late (X06), bad key (X07), forged outbound (X08), lost answer (X09) [done]
8. real HTTPS process instead of the in-process test client [done, L01, L02]
9. demo launcher and rehearsal [done]

Stub based first: the cross part tests run Part B in process behind its real routes through Flask's test
client, on a manual clock. Only after they passed did the live test and the demo use the real server.

## Deliverables

`INTERFACE.md`, `DECISIONS.md`, `CHANGES.md`, `INTEGRATION_LOG.md`, `adc_bridge.py`, `run_adc.py`,
`run_demo.py` and `run_demo`, `DEMO.md`, `tests/test_cross_part.py`, `tests/test_live_https.py`,
root `README.md` and `CLAUDE.md`. [all done]

## Missing information

1. `deliverable3/` (draft docx, pdf, `build_d3.py`, `docx_lib.py`) is not on this machine. Phase C waits for it.
   The Part A report content is prepared in `D3_REPORT_CONTENT.md` so it can go into `build_d3.py` directly.
2. Part A's Deliverable 1 and 2 documents are not here; Part A facts come from its code and docstrings.
3. Part A team members other than the single git author.
4. Who did what in Deliverable 3 (human division).
