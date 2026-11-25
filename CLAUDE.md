# CLAUDE.md

Two independently graded course projects (McMaster 3K04) joined for Deliverable 3. Read `README.md` first.

## Rules for changes

- Do not rewrite, reformat or rename anything in `part_a_adc/` or `part_b_monitoring/`. Public names, file names,
  test names and table row labels are cited by the design documents. Make the smallest change and record it in
  `integration/CHANGES.md` with the reason; record decisions in `integration/DECISIONS.md`.
- Line endings: the repo stores LF and Windows checkouts use `core.autocrlf=true`. Do not set it to false; it
  makes every line of a CRLF working file look changed.
- All translation between the two wire formats belongs in `integration/adc_bridge.py`. Part B's wire format is
  hidden in `monitoring/adc_comm.py` (M10) and its transport in `monitoring/web/adc_gateway.py` (M15). Part A's
  is in `adc/decisions/message_codec.py`.
- Never report a test result you did not run. `integration/INTEGRATION_LOG.md` is append only, with time,
  symptom, root cause, fix, minutes.

## Layout

- `part_a_adc/adc/` hardware (1.1), behaviour (1.2), decisions (1.3), `app.py` (Module 1, the only module
  holding several references), `main.py` (console). Tests in `part_a_adc/tests/`.
- `part_b_monitoring/monitoring/` modules M01 to M17 (`domain` .. `system`), `web/` for M15 and M16. Timings in
  `policy.py`. Tests in `part_b_monitoring/tests/`.
- `integration/` adapter, `run_adc.py` (Part A console wired to the adapter), `run_demo.py`, `tests/`.

## Commands

    python integration/run_demo.py [--scripted] [--time-scale N] [--port N]
    cd part_a_adc && python -m unittest discover -s tests -t .
    cd part_b_monitoring && python -W ignore -m unittest discover -s tests
    python -W ignore -m unittest discover -s integration/tests
    cd part_b_monitoring && python -m coverage run --branch --source=monitoring -m unittest discover -s tests
    cd part_a_adc && python -m coverage run --branch --source=adc -m unittest discover -s tests -t .
