# Changes to either part (Deliverable 3)

Baseline: commit `b819411` holds both parts exactly as imported.

## Part A (`part_a_adc/`)

| File | What | Why |
|---|---|---|
| `adc/app.py` | Added `pending_wire()` (7 lines) after `pending_messages()`. Returns `[(msg_id, wire)]` for unacknowledged messages, oldest first, encoded by Part A's own `MessageCodec`. Read only. | The adapter needs the queued payloads (D3-03). No existing line changed. |
| `tests/test_safety_requirements.py` | New file, 6 tests: `test_SR_ADC_03_lock_mismatch_enters_fault_at_two_seconds`, `test_SR_ADC_04_session_expires_at_120_seconds_idle`, `test_SR_ADC_04_inactive_account_is_refused`, `test_SR_ADC_05_queue_clears_only_on_matching_ack_in_order`, `test_SR_ADC_05_link_loss_keeps_messages_and_never_changes_mode`, `test_SR_ADC_05_link_declared_down_at_fifteen_seconds`. | SR-ADC-03, 04 and 05 had no unit test. The 21 Deliverable 2 tests are unchanged. |

## Part B (`part_b_monitoring/`)

No changes.

## New, outside both parts

`integration/` (adapter, launchers, tests, documents), root `README.md`, `CLAUDE.md`, `.gitignore`.
