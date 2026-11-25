# Deliverable 3 report content prepared from the code (to go into build_d3.py)

The draft `deliverable3/` (docx, pdf, `build_d3.py`, `docx_lib.py`) was not on this machine on 25 November 2026,
so Phase C could not edit it. Everything below is written from Part A's code, Part B's code and runs recorded in
`INTEGRATION_LOG.md`, ready to paste into `build_d3.py`. Requirement IDs are the ones Part A's own docstrings cite.

## Numbers to correct in the draft (Part B)

| Item | Draft said | Measured 25 Nov 2026 in the merged repo |
|---|---|---|
| Part B tests | 341 passing | 374 passing (`python -W ignore -m unittest discover -s tests`) |
| Branch coverage | 88.2% (508 of 576) | 88.8% (579 of 652) |
| Statement coverage | 95.8% (1,916 of 2,000) | 96.2% (2,056 of 2,138) |

Command: `coverage run --branch --source=monitoring -m unittest discover -s tests`, coverage 7.17.5, Python 3.13.14.
The final run's figures are in `runs/final_partB.txt`.

## Section 1 additions

**Part A, the ADC.** Python, standard library only, about 1,920 lines in 18 modules split by information
hiding: hardware hiding (clock, credential reader, drawer sensor, lock driver, removal sensor), behaviour hiding
(authorization, dispense transaction, drawer state, link state, mode manager, session manager, timing policy)
and software decisions (inventory store, audit log store, outbound queue, message codec, UI presentation).
`app.py` (Module 1) is the only module holding several references, so the USES relation is acyclic. A console
(`main.py`) drives it with simulated hardware. It authenticates staff by card, unlocks one drawer per confirmed
dispense, moves stock only when removal and closure are both confirmed, enters FAULT on a jammed lock, and keeps
working when the Monitoring System link is down.

**Part A members.** Avneet Singh Nijjer is the only author in the Part A repository's git history (3 commits,
2 to 4 November 2026). *Inferred from git history, confirm.* **TO COMPLETE (team to fill):** the other Part A
members and who built what.

**Division of Deliverable 3.** Claude Code (an AI coding assistant) did the code integration on 25 November 2026:
the adapter, launchers, cross part tests, six added Part A tests, the integration documents and the coverage
runs, under the direction of Pranoy Mukherjee. **TO COMPLETE (team to fill):** how the team divided the rest
(FMEA review, demo, document sections).

**Integration outcome (paragraph).** The two parts run together over HTTPS. A thin adapter on the ADC side
translates Part A's outbound queue into Part B's signed messages; Part B is unchanged and Part A gained one
read-only method. End to end, a dispense on the ADC appears on the dashboard with the nurse's ID and the new
stock count, lock and drawer faults raise alarms that clear when a technician resets the cabinet, the ADC's mode
is shown, and a pharmacist's threshold change reaches the ADC and is confirmed. When the link drops, the ADC
keeps dispensing and stores its messages, the Monitoring System marks it Offline with Stale inventory after 30
s, and on reconnection the stored messages are replayed in order and recorded Late. Seventeen automated cross part tests
pass. Refill and maintenance requests are not integrated because Part A has no request function, and the ADC
acknowledges threshold changes without acting on them because Part A has no low-stock logic.

## Section 2, ADC: FMEA

| ID | Component | Failure mode | Causal factors | Immediate effect | System effect (hazard / harm) |
|---|---|---|---|---|---|
| F-ADC-01 | Dispensing control (`app.confirm_dispense`, lock driver) | A second drawer unlocks while one is open | Missing one-drawer guard; stale `_open_drawer` after a fault | Two medications accessible at once | Wrong medication taken: patient harm (H1 wrong-medication dispense) |
| F-ADC-02 | Mode manager and lock driver | A drawer is unlocked or stays unlocked in FAULT or MAINTENANCE | Mode check skipped; locks not re-commanded on mode change | Cabinet open while it is known to be unsafe or under service | Unsecured controlled substances: diversion, patient harm |
| F-ADC-03 | Dispense transaction and inventory store | Stock decremented without confirmed removal and closure, or decremented twice | Wrong function table row; retry of the same transaction | Recorded count lower than the real count | Count drift: stock-out of a needed drug or undetected diversion |
| F-ADC-04 | Hardware poll and lock driver | Jammed lock (feedback disagrees with command) not detected | Missing timeout; poll not run | Drawer believed locked while open, or the reverse | Unsecured controlled substances; a fault not escalated |
| F-ADC-05 | Session manager and credential reader | A session stays open after the user walks away, or an inactive account logs in | No idle timeout; inactive list not checked | Next person dispenses under someone else's identity | Unattributable dispense, diversion; audit record wrong |
| F-ADC-06 | Outbound queue and link state | A dispense or alarm message is lost or sent twice when the link fails | Message dropped on send failure; acknowledgement matched to the wrong message | Monitoring System never learns of an event, or counts it twice | Pharmacy acts on wrong stock; alarm missed at central monitoring |
| F-ADC-07 | Audit log store | A credential is written into the audit record | Detail field copied from the credential reader | Secret stored in plain text | Credential theft, impersonation |

## Section 2, ADC: safety requirements

| ID | Requirement | Mitigates |
|---|---|---|
| SR-ADC-01 | At most one drawer shall be commanded unlocked at any time, and only in NORMAL mode after a confirmed request from a permitted role. Leaving NORMAL shall lock every drawer. | F-ADC-01, F-ADC-02 |
| SR-ADC-02 | Inventory shall be decremented only when removal and closure are both confirmed (row R3), and at most once per transaction ID. Any other outcome leaves the count unchanged and is reported UNCERTAIN. | F-ADC-03 |
| SR-ADC-03 | A lock feedback mismatch lasting 2 s shall inhibit that drawer, lock every drawer and put the cabinet in FAULT, which only an authorised role with a passing self test can clear. | F-ADC-04 |
| SR-ADC-04 | A session shall end after 120 s without activity, an inactive account shall never open a session, and no credential shall be written to the audit log. | F-ADC-05, F-ADC-07 |
| SR-ADC-05 | Every outbound message shall stay queued, in order, until an acknowledgement carrying its own ID arrives. Loss of the link shall not change the cabinet mode or stop local dispensing. | F-ADC-06 |

## Section 3, ADC: testing plan

**Criterion.** Branch coverage, because Part A's safety rules are decisions (the R1 to R6 completion table, the
mode and drawer transition tables, the timeout predicates) and every branch of them is a distinct safety
outcome that statement coverage could miss.

**Measured.** Part A's own 27 tests, with no Monitoring System involved: branch 120 of 204 (58.8%), statement
691 of 846 (81.7%), excluding the console `main.py`; with `main.py` 120 of 276 branches. Command:
`coverage run --branch --source=adc -m unittest discover -s tests -t .`, 25 November 2026. The lowest modules
are `message_codec.py` (0 of 8 branches, exercised only by the cross part tests), `link_state.py` (2 of 8) and
`app.py` (42 of 86, mostly console and maintenance paths). The target is not met; the gap is reported as is.

**Test cases.** Run 25 November 2026, `python -m unittest discover -s tests -t .` in `part_a_adc/`.

| ID | Test case (inputs) | Expected result | Traces to | Observed result | Pass/Fail |
|---|---|---|---|---|---|
| T-ADC-01 | `test_INV_G3_only_one_drawer_unlocked`: nurse confirms one dispense | Exactly one drawer reported UNLOCKED | SR-ADC-01, FR-DSP-03 | One drawer UNLOCKED | Pass |
| T-ADC-02 | `test_second_dispense_refused_while_a_drawer_is_open`: second request confirmed while D1 open | Refused, "Another drawer is still open" | SR-ADC-01, FR-DSP-03 | Refused with that text | Pass |
| T-ADC-03 | `test_INV_G1_no_unlock_outside_normal`: technician enters MAINTENANCE, nurse requests | Request refused, "mode blocks" | SR-ADC-01, FR-MOD-02 | Refused, mode blocks | Pass |
| T-ADC-04 | `test_R3_removal_and_closure_confirmed_completes`: removal true, closure true | Row R3, status COMPLETED | SR-ADC-02, FR-DSP-04 | R3, COMPLETED | Pass |
| T-ADC-05 | `test_INV_T2_uncertain_never_decrements` (off normal): drawer closed, removal never confirmed | UNCERTAIN, stock unchanged | SR-ADC-02, NFR-SAF-02 | UNCERTAIN, stock 12 before and after | Pass |
| T-ADC-06 | `test_INV_I2_decrement_is_idempotent`: same transaction ID decremented twice, stock 5 | First applied, second refused, stock 3 | SR-ADC-02, NFR-REL-02 | True, False, 3 | Pass |
| T-ADC-07 | `test_SR_ADC_03_lock_mismatch_enters_fault_at_two_seconds` (boundary, added D3): D2 feedback jammed, poll at 1.9 s and 2.0 s | NORMAL at 1.9 s; at 2.0 s D2 INHIBITED, all locked, FAULT, dispense refused | SR-ADC-03, FR-FLT-02 | As expected | Pass |
| T-ADC-08 | `test_SR_ADC_04_session_expires_at_120_seconds_idle` (boundary, added D3): idle 119 s then 120 s | Request accepted at 119 s, refused "session expired" at 120 s | SR-ADC-04, FR-AUTH-04 | As expected | Pass |
| T-ADC-09 | `test_INV_A2_credentials_are_never_logged`: audit record with a `credential_token` key | Append refused | SR-ADC-04 | AssertionError raised | Pass |
| T-ADC-10 | `test_SR_ADC_05_link_loss_keeps_messages_and_never_changes_mode` (off normal, added D3): 16 s silence, then a dispense | LINK OFFLINE, MODE NORMAL, dispense COMPLETED, DISPENSE_RESULT and INVENTORY_UPDATE queued | SR-ADC-05, FR-FLT-03, FR-COM-04 | As expected | Pass |

Also run and passing: R1, R2, R4, R5, R6, table completeness, mode and drawer table totality, INV-D1, INV-I1, the
inactive-account, ordered-ack and 15 s link boundary tests (27 in all).

**Integration within Part A.** Bottom up along the USES relation: hardware hiding modules first behind their
`Simulated*` drivers, then behaviour and decision modules (unit tests drive `DispenseTransaction`,
`InventoryStore`, `OutboundQueue`, `LinkStateMachine` directly), then `AdcApplication` with all simulated
hardware. The Monitoring System is replaced by a stub: tests feed `monitor_response` hand-written ACK messages
and read the queue, and `SimulatedClock` is the driver for every timeout.

## Section 4, integration report (about one page)

**Interface.** As in `INTERFACE.md`: ADC to Monitoring status (with a 10 s heartbeat), inventory, dispensing,
alarm_notice, alarm_clear and config_ack; Monitoring to ADC configuration, inventory_request and request_reply.
JSON over HTTPS, `POST /adc/messages` and a signed `GET /adc/outbox` poll, every message signed with HMAC-SHA256
under a key per ADC. Part A's Deliverable 2 format (`{"v","id","kind","payload"}`, no signature, no timestamp,
string IDs, per-message ACKs) is translated by one adapter file on the ADC side.

**Approach and order.** Part B already had a tested signed interface and was the receiver, so it stayed
unchanged and Part A conformed through the adapter. Order: status, inventory, alarms, dispensing,
configuration, then off normal cases. Each step was first tested with Part B in process behind its real HTTP
routes on a manual clock (a stub for the network and for time), then against the real HTTPS server, then in the
demo. Requests were not integrated (Part A has none).

**Responsibilities.** **TO COMPLETE (team to fill).** Claude Code wrote the adapter and tests on 25 November 2026.

**Testing together.** 17 automated cross part tests in `integration/tests/` (14 in process, 3 against the real
server): alarm shown (X01), dispense and inventory shown (X02), UNCERTAIN alarm (X03), threshold change
acknowledged (X04, and through the real dashboard L03), link drop with Offline, Stale, local dispensing and
recovery (X05, X12), replay recorded Late (X06), wrong key rejected and the message kept (X07), forged outbound
ignored (X08), lost answer retried as a Duplicate (X09), restart without ID reuse (X13). Three deliberate faults
in the adapter checked that the tests can fail.

**What went wrong.** (1) Part B has no acknowledgement message, while Part A leaves its RESYNC state only when
an ACK arrives with its queue empty. Our first tests always had messages queued, so a deliberately broken
adapter that never told Part A the link was alive still passed. Root cause: two link-state designs made
separately, each assuming the other side's acknowledgement style. (2) A threshold confirmed on the dashboard
stayed Unconfirmed when two ADC processes started in the same second: the adapter's boot-based message IDs
repeated, and Part B silently discards repeated IDs while still answering "accepted". Root cause: the ID rule
was chosen without considering that Part B remembers IDs for ever and that Part A restarts its counter at 1.
(3) Part A's messages had no timestamp and its clock counts from an arbitrary zero, while Part B classes
anything more than 30 s old as Late. Root cause: the Deliverable 1 interface assumption never fixed the time
base or who stamps a message. Fixed by stamping wall time when the adapter first sees a message.

**What would need to be different.** (1) Agree one written interface document, with example messages and a
shared test vector for the signature, before either part's Deliverable 2. (2) Fix message identity and time in
that document: who assigns IDs, their range and their lifetime across restarts, and the epoch and clock of
timestamps. (3) Each sub team ships a stand-in of the other side built from that document (Part B's simulator
did this; Part A had none), and both run the other team's stand-in in CI.

## Section 5, ethics (optional ADC example, if it is stronger than the draft's)

Conflict: nurses need medication fast, and every safeguard slows them. Part A makes a nurse confirm a screen
before any drawer unlocks and refuses to move stock when removal is not sensed, which costs seconds. The
integration added a second cost: an UNCERTAIN dispense now raises an alarm at the Monitoring System that a
pharmacist must resolve. We keep both because the PEO Code of Ethics puts the duty to public welfare first: a
wrong or unrecorded dispense of a controlled drug harms the patient, while a few seconds of delay rarely does.
The ADC also keeps dispensing when the Monitoring link is down, so central monitoring never blocks care.

## Section 6, new assumptions and decisions

From `DECISIONS.md`: D3-01 adapter on the ADC side, D3-02 Part B unchanged, D3-03 one read-only Part A method,
D3-04 Part A's queue is the only buffer, D3-05 a rejected message is held, D3-06 threshold acknowledged but not
acted on, D3-07 UNCERTAIN becomes an alarm and denials stay local, D3-08 alarm clear from drawer state, D3-09
time-based increasing message IDs, D3-10 timestamp at first sight, D3-11 the ADC runs as ADC-01, D3-12 the lab
certificate is accepted. New assumption: one medication per drawer, so drawer `D<n>` is slot `n`.
