# Deliverable 3 demo

Rehearsed 25 November 2026 (outputs in `runs/rehearsal_scripted.txt` and `runs/rehearsal_interactive.txt`).

## Before the lab

    python -m pip install flask          # Part B's only dependency
    git clone https://github.com/Pranoym17/3k04-Final.git && cd 3k04-Final

## Launch both parts (one command)

    python integration/run_demo.py

This starts the Monitoring System on https://127.0.0.1:5000 with a fresh seeded history and opens Part A's ADC
console in the same terminal, connected as ADC-01. Open https://127.0.0.1:5000 in a browser, accept the
self-signed certificate, log in as `admin1` / `12345` (or `pharm1` for thresholds).

Separate windows instead, if you want the Monitoring log visible:

    cd part_b_monitoring && python run_monitoring.py --fresh --seed --demo
    python integration/run_adc.py --insecure            # second terminal, from the repo root

`--time-scale 3` on `run_demo.py` divides Part B's 30 s timers by 3 (Offline after 10 s). The ADC's own 15 s
link timeout is Part A's and does not scale.

## Scenario 1, end to end (about 2 minutes)

At the `adc>` prompt:

    login card-nurse
    dispense MORPH-10MG 2
    confirm
    take
    close

Dashboard: ADC-01 Online, slot 1 MORPH-10MG goes 12 to 10, the History shows `dispensing` Accepted with user
`u-nurse-01`.

    lockfault D2

Dashboard: high alarm LOCK_MISMATCH on ADC-01, ADC mode Fault, fault alarm. ADC banner `MODE: FAULT`.

    login card-tech
    clearfault

Dashboard: LOCK_MISMATCH alarm cleared, mode Normal.

Threshold: on the dashboard (pharm1), open ADC-01 and set slot 1 threshold to 11. It shows Unconfirmed, then
Confirmed within a second; slot 1 (10) now shows Low. `net` on the ADC shows "configuration slot 1 threshold 11:
ADC says Accepted CONFIG."

## Scenario 2, off normal: the link drops (about 1.5 minutes)

    netdown
    login card-nurse
    dispense PARA-500MG 1
    confirm
    take
    close
    queue

The ADC dispenses normally; `queue` lists the DISPENSE_RESULT and INVENTORY_UPDATE held. After 15 s the 5 s poll
prints `LINK OFFLINE, local operation continues` and the banner shows the stored count. After 30 s the dashboard
shows ADC-01 Offline, inventory Stale, and a communication alarm.

    netup
    status

The ADC replays oldest first, banner back to `LINK: ONLINE`, queue empty. Dashboard: ADC-01 Online, History shows
the replayed `dispensing` and `inventory` as **Late** ("replayed after an outage"), followed by a fresh status
and inventory Accepted. Slot 2 shows 29.

Optional: stop the Monitoring System (Ctrl+C in its window) instead of `netdown`; the ADC behaves the same and
replays when it is restarted without `--fresh`.

## Do not use in the integrated demo

`ack`, `linkup`, `linkdown`, `badmsg` are Part A's Deliverable 2 commands that pretend to be the Monitoring
System. `ack MSG-n` removes a message from the ADC queue without sending it.

## What is and is not integrated

Integrated and tested across the boundary: status and heartbeat, inventory, dispensing, alarms raised and
cleared (lock and drawer faults, UNCERTAIN dispenses), threshold configuration with acknowledgement, inventory
request, Offline and Stale on link loss with local dispensing continuing, ordered replay recorded Late,
duplicate on retry, wrong key rejected, forged outbound ignored, HTTPS.

Not integrated: refill and maintenance requests and their replies (Part A has no request function). The
threshold is acknowledged but the ADC does not act on it (Part A has no low-stock function; Part B computes
Low itself). Denied dispenses and failed logins stay on the ADC (no Part B message type).
