# ADC to Monitoring System interface (as integrated, Deliverable 3)

This is the written contract both parts run against today. Part B's side is unchanged from its
Deliverable 2 design (`monitoring/adc_comm.py` M10, `monitoring/web/adc_gateway.py` M15). Part A's side is
unchanged except one read-only accessor (`AdcApplication.pending_wire`). The translation between the two
lives in one file, `integration/adc_bridge.py` (the Monitoring Link Adapter), which runs in the ADC process.

## 1. Transport

| Item | Value |
|---|---|
| Inbound to Monitoring | `POST /adc/messages`, body one JSON message. Answer `{"result": "accepted"/"rejected", "reason": "", "outbox": [...]}`. HTTP 200 or 400. |
| Outbound to ADC | Carried in `outbox` of every answer, and by a signed poll `GET /adc/outbox?adc_id=&ts=&auth=` with `auth = HMAC-SHA256(key, "outbox|<adc_id>|<ts>")`. The adapter polls every 0.5 s cycle. |
| Security | HTTPS (self-signed certificate in the lab; the adapter's `--insecure` accepts it). Every message carries `auth`. |
| Identity | ADC ID `ADC-01`, key `demo-key-ADC-01` (registered by Part B's demo seed). |

## 2. Message envelope (every message, both directions)

`adc_id` (string, letters digits `-`, 1 to 32), `message_id` (integer 0 to 10^12), `timestamp` (seconds since
the epoch, float), `type`, `auth`. `auth` is HMAC-SHA256, under the per-ADC key, of the canonical JSON of all
other fields: keys sorted, separators `,` and `:`, ASCII. Fields not listed for the type are refused.

## 3. ADC to Monitoring

| Part B type | Fields | Made from (Part A) | Mapping |
|---|---|---|---|
| `status` | `mode`: Normal, Maintenance, Fault | `MODE_CHANGE {from,to}`, and a heartbeat every 10 s | NORMAL to Normal, MAINTENANCE to Maintenance, FAULT to Fault. STARTUP is not sent. |
| `inventory` | `slots: [{slot, medication, quantity}]` | `INVENTORY_UPDATE {item,qty,rev}`; also on first contact, on every reconnect and on `inventory_request` | Full snapshot from `inventory_snapshot()`. Drawer `D<n>` is slot `n`. `medication` is the Part A item ID. |
| `dispensing` | `slot, medication, quantity, adc_user_id` | `DISPENSE_RESULT` with status COMPLETED | `adc_user_id` is the Part A user ID found in the audit record of the same transaction. |
| `alarm_notice` | `alarm_id, alarm_type, severity` | `ALARM {type,drawer,critical}`; `DISPENSE_RESULT` with status UNCERTAIN | `alarm_id` is the Part A message ID. critical true is high, false is medium. UNCERTAIN is `dispense UNCERTAIN TX-nnnn`, medium. |
| `alarm_clear` | `alarm_id` | Drawer returns to IN_SERVICE in `drawer_report()` | Clears every alarm raised for that drawer. |
| `config_ack` | `config_id, slot, threshold` | Part A accepted the configuration through `monitor_response` | Sent only when Part A answers "Accepted". |
| `request` | `request_id, request_type, slots` | none | **Not integrated.** Part A has no refill or maintenance request function. |
| none | | `AUTH_FAILED`, `DISPENSE_RESULT` DENIED | Local to the ADC. Acknowledged in Part A only after a successful exchange in the same cycle. |

`message_id`: milliseconds since epoch second 1 700 000 000 when the adapter first sees the message, raised to
one more than the previous ID if needed, so IDs only increase, also across an ADC restart. Part B accepts IDs up
to 10^12, which lasts until 2055. A Part A message gets its ID and timestamp once and keeps them on every retry,
so a retry is a Duplicate at Part B.

`timestamp`: wall clock when the adapter first saw the Part A message, not when it was finally sent. A message
held through an outage longer than 30 s is therefore recorded Late by Part B, as Part B's design requires.

## 4. Monitoring to ADC

| Part B type | Fields | Delivered to Part A as | ADC answer |
|---|---|---|---|
| `configuration` | `config_id, slot, threshold` | `{"v":1,"kind":"CONFIG","payload":{...}}` to `monitor_response` | `config_ack` when Part A says Accepted. The threshold is held by the adapter (Part A has no low-stock function). |
| `inventory_request` | none | not passed on | Fresh `inventory` snapshot. |
| `request_reply` | `request_id, decision, reason, slot` | `{"kind":"REQUEST_REPLY"}` to `monitor_response` | none. Never produced today because Part A sends no request. |
| any, bad `auth` | | dropped by the adapter | none, logged `IGNORED`. |

Delivery acknowledgement: Part B has no ACK message. "accepted" in the HTTP answer is the acknowledgement. The
adapter turns it into Part A's own `{"kind":"ACK","payload":{"ack_id":"MSG-n"}}`, which is the only way a
message leaves Part A's outbound queue. After every successful cycle the adapter also sends
`{"kind":"ACK","payload":{"ack_id":""}}`, which acknowledges nothing but tells Part A's link state that the
Monitoring System is alive (this ends RESYNC when the queue is already empty).

## 5. Timing at the boundary

| Rule | Owner | Value |
|---|---|---|
| ADC heartbeat (status) | adapter | 10 s (Part B assumption A1) |
| Monitoring marks ADC Offline, inventory Stale | Part B `policy.py` | 30 s without a valid message |
| Message recorded Late | Part B | ADC timestamp more than 30 s before receipt. A Late message never restarts the offline timer. |
| ADC declares its own link down | Part A `timing_policy.py` | 3 missed 5 s heartbeats = 15 s |
| Threshold change Failed without config_ack | Part B | 60 s |
| Threshold change refused | Part B | ADC not Online |
| Unacknowledged high alarm escalates | Part B | 2 min |

## 6. Failure behaviour

| Situation | ADC | Monitoring |
|---|---|---|
| Link down | Keeps dispensing (Part A A-05). Messages stay in its queue in order. Banner shows OFFLINE and the stored count after 15 s. | Offline and Stale after 30 s, communication alarm. |
| Link back | Replays oldest first, then a fresh status and inventory. RESYNC then ONLINE. | Replayed messages Late; fresh status and inventory bring it Online. |
| Answer lost after Part B recorded the message | Retries with the same ID. | Duplicate, discarded, answer "accepted". |
| Wrong key | Message rejected, not acknowledged, queue held (count grows on the banner). | Rejected, "bad authentication code" in History. |
| Forged outbound message | Dropped by the adapter. | n/a |
