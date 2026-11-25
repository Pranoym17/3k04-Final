# Integration log

Kept while working on 25 November 2026 (times local, EST). Not tidied afterwards. Work done by Claude Code.
Machine: Windows 11, Python 3.13.14, Flask 3.1.3, coverage 7.17.5.

## Timeline

| Time | Step | Result |
|---|---|---|
| 15:12 | Found `3k04-Final` (target, README only), `Software3K04-D2` (Part A, 3 commits by one author), `monitoring_system` (Part B, 1 uncommitted README edit). `deliverable3/` draft not on disk. | Discovery only. |
| 15:13 | Ran Part A tests in its own repo. | 21 tests OK. |
| 15:15 | Ran Part B tests in its own repo: `python -W ignore -m unittest discover -s tests`. | 374 tests OK in 159.7 s (the prompt said 341; Part B grew since). |
| 15:18 | Branch `integration-d3`, copied both parts, safety commit `b819411`. | See problem 1. |
| 15:23 | Part B coverage in merged repo, `coverage run --branch --source=monitoring -m unittest discover -s tests`. | 374 OK. Branch 579/652 (88.8%), statement 2056/2138 (96.2%). |
| 15:24 | Part A coverage baseline (21 tests). | OK. 56% combined (`main.py` included). |
| 15:25 | Added `AdcApplication.pending_wire()`; wrote adapter and 12 cross part tests. | See problem 2 (caught before running). |
| 15:26 | First cross part run. | Discovery error, problem 3. Then 12/12 OK. |
| 15:26 | Mutation check: three deliberate adapter faults. | 2 of 3 caught; problem 4. Added X12. 13/13 OK, mutant killed. |
| 15:27 | Live test against `run_monitoring.py` over HTTPS. | 2/2 OK. |
| 15:28 | Scripted rehearsal `python integration/run_demo.py --scripted --port 5077`. | Both scenarios behaved as designed. Output in `runs/rehearsal_scripted.txt`. |
| 15:29 | Interactive rehearsal with piped commands. | Works. Problem 7. Output in `runs/rehearsal_interactive.txt`. |
| 15:30 | `git diff --stat` after the Part A edit. | Problem 5. |
| 15:31 | Six new Part A tests for SR-ADC-03..05. | Problem 6, then 27/27 OK. |
| 15:33 | Live test L03: threshold set through the real dashboard over HTTPS. | Failed after L01 ran; problem 8. |
| 15:35 | New message ID scheme, regression test X13. | 17/17 OK. Old scheme fails X13 and L03. |

## Interface mismatches found in discovery (read from the code, before any run)

| Item | Part A today | Part B today | Fix, side |
|---|---|---|---|
| Transport | None. `flush_queue()` only prints; nothing leaves the process. | HTTPS POST and signed GET. | Adapter, Part A side. |
| Envelope | `{"v":1,"id","kind","payload"}` unsigned | flat fields, `auth` HMAC-SHA256 | Adapter. |
| Message ID | String `MSG-00001`, restarts at 1 on boot | Integer 0..10^12, duplicates detected forever per ADC | Adapter mapping, D3-09. |
| Timestamp | None in the message. Part A clock is monotonic from an arbitrary zero (SimulatedClock starts at 0). | Epoch seconds; more than 30 s old is Late | Adapter stamps wall time at first sight, D3-10. |
| Acknowledgement | Expects an inbound `ACK` message per message ID | No ACK message; "accepted" in the HTTP answer | Adapter converts. Problem 4. |
| Kinds | MODE_CHANGE, INVENTORY_UPDATE, ALARM, DISPENSE_RESULT, AUTH_FAILED | status, inventory, alarm_notice, alarm_clear, dispensing, request, config_ack | Mapping table in INTERFACE.md. |
| Location | Drawer `D1` | Slot integer 1..99 | `D<n>` is slot n. |
| Modes | STARTUP, NORMAL, MAINTENANCE, FAULT | Normal, Maintenance, Fault | Map; STARTUP not sent. |
| User in dispense | Not in the payload, only in the audit record | `adc_user_id` required | Adapter reads `audit_tail()`. |
| Heartbeat | None outbound | Offline after 30 s silence | Adapter sends status every 10 s. |
| Alarm clear | Not emitted | `alarm_clear` | From drawer state, D3-08. |
| Request / reply | No such function | refill, maintenance | Not integrated. |
| Threshold config | No such function | configuration, config_ack within 60 s | Partly, D3-06. |

## Problems

**Problem 1, 15:18, 1 min.** Symptom: `git commit` failed, "Author identity unknown". Root cause: no global git
identity on this machine. Fix: set `user.name` and `user.email` in the new repo only, from the existing Part B
commit author.

**Problem 2, about 15:25, 2 min (near miss, caught before running).** Symptom: none yet. While writing the
tests I checked the adapter's message ID formula, `(boot - 1.7e9) x 10000`, against Part B's
`_int(message_id, 0, 10**12)`. At epoch 1.8e9 (January 2027, and the start value of Part B's `ManualClock`)
the IDs pass 10^12 and every message would be rejected as bad format. Root cause: I picked the multiplier for
collision room without checking the receiver's range. Fix: multiplier 1000, valid until 2055. (This formula was itself wrong; see problem 8.)

**Problem 3, 15:26, 1 min.** Symptom: `unittest discover -s integration/tests -t integration` raised "Start
directory is not importable". Root cause: `-t` made `integration` the top-level package, which needs
`__init__.py` files. Fix: run without `-t`; the test file sets its own `sys.path`.

**Problem 4, 15:26, 4 min.** Symptom: the 12 cross part tests passed first time, so I broke the adapter on
purpose three ways. Stamping time at send (X06 failed) and acknowledging a rejected message (X07 failed) were
caught. Removing the adapter's liveness ACK was not caught. Root cause: Part A leaves RESYNC only when an
`ACK` arrives while its queue is empty (`LinkStateMachine.note_queue_drained` is called only from the ACK
branch of `monitor_response`), and Part B has no ACK message at all. Test X05 always had messages queued, so
the empty-queue path was never exercised. Without the liveness ACK an ADC that queued nothing during an
outage would stay OFFLINE for ever. Fix: kept the liveness ACK, added `test_X12_adc_link_returns_online_after_an_outage_with_nothing_queued`, confirmed it fails with the
mutant and passes without.

**Problem 5, 15:30, 3 min.** Symptom: `git diff --stat` showed 931 changed lines in `app.py` for a 7-line
addition. Root cause: I had set `core.autocrlf=false` in the new repo. The files were copied from a Windows
checkout with CRLF endings while the index held LF, so every line looked changed. Committing would have
reformatted Part A wholesale, against the "never reformat" rule. Fix: removed the local setting (system
default `true`); diff back to 7 lines.

**Problem 6, 15:31, 2 min.** Symptom: two new Part A tests failed, `'mode_blocks' not found`. Root cause: the
tests expected the internal reason code; Part A's `UiPresentation` shows it as `reason    : mode blocks`.
Fix: assert on the shown text. Test bug, not a Part A bug.

**Problem 7, 15:29, 1 min.** Symptom: in the piped interactive rehearsal the "Monitoring System on ..." line
appeared after the session ended. Root cause: stdout is block buffered when not a terminal, and the ADC
console runs as a child process. Fix: `flush=True`.

**Problem 8, 15:33, 5 min.** Symptom: live test L03 (pharmacist sets slot 1 threshold to 11 on the real
dashboard) passed alone but failed after L01: the ADC logged "configuration ... ADC says Accepted CONFIG" and
sent `config_ack`, Part B answered "accepted", yet the dashboard still showed Unconfirmed. Root cause: the
adapter's ID base was `int(boot - 1.7e9) x 1000`. L01's and L03's ADCs started within the same second, got the
same base, and L03's `config_ack` reused an ID that L01 had already sent. Part B classed it Duplicate and
discarded it, and by design answers Duplicate with "accepted" so a sender stops retrying, so the loss was
silent at the ADC. A real cabinet restarting quickly (for example in a crash loop) would lose messages the same
way. Fix: IDs are increasing milliseconds since the ID epoch (D3-09). Regression test
`test_X13_adc_restarted_within_a_second_does_not_reuse_message_ids` fails on the old scheme and passes now.

## Risks still open

* One malformed message holds Part A's queue until fixed (D3-05). Visible, not silent.
* Two items in one drawer would send a duplicate slot and Part B rejects the report. Part A's demo stock has one item per drawer.
* The threshold acknowledged by the ADC changes nothing on the ADC (D3-06).
* `alarm_clear` for an UNCERTAIN dispense is never sent; the operator acknowledges it on the dashboard.
