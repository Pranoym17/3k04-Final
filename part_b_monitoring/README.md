# Monitoring System (Part B), Sub Team 29

Python 3.9 or later. Flask is the only Python dependency. HTTPS certificate generation uses OpenSSL
on PATH, or the copy included with Git for Windows.

    python3 -m pip install flask
    python3 run_monitoring.py --seed --demo      # dashboard on https://127.0.0.1:5000
    python3 run_simulator.py --insecure          # ADC stand-in on http://127.0.0.1:5001

The first command creates the demonstration accounts and registers ADC-01 to ADC-03. `--demo` adds the
storage-failure button to the administrator page. `--fresh` deletes the selected history database first
(default `data/history.db`).

| User | Role | Password |
|---|---|---|
| admin1 | Administrator | 12345 |
| pharm1 | Pharmacist | 12345 |
| nurse1 | Nurse | 12345 |
| tech1 | Technician | 12345 |
| tech2 | Technician | 12345 |

The ADC keys are `demo-key-ADC-01`, `demo-key-ADC-02` and `demo-key-ADC-03`. These are demonstration
credentials only.

`--seed` also updates existing demo accounts to password `12345`, preserving their roles and disabled state.
This short password is allowed only by demo seeding; ordinary account creation still requires eight characters.

Options of `run_monitoring.py`: `--db PATH`, `--host`, `--port`, `--seed`, `--demo`, `--time-scale N` (divides
operational deadlines and the heartbeat by N; scheduler cadence and signed-poll freshness stay unchanged),
`--tls` (the default: HTTPS with a self-signed certificate), `--fresh`. The launch command always uses HTTPS.
Options of `run_simulator.py`: `--url`, `--adcs ADC-01,ADC-02`, `--time-scale N` (use the same N as the Monitoring
System), `--insecure` (accept the self-signed certificate), `--port`.

HTTPS is the default. Accept the self-signed certificate in the demonstration browser; the simulator's
`--insecure` accepts it for the lab. Explicit TLS also works:
`python3 run_monitoring.py --seed --demo --tls` and `python3 run_simulator.py --url https://127.0.0.1:5000 --insecure`.

ADC responses use only `accepted` or `rejected`, with no validation diagnostic. Duplicate and Late outcomes
and detailed rejection reasons remain in History. Request replies still include their decision and reason.

## Using the dashboard

With `--demo`, choose one of the five demo accounts to enter immediately without typing a password.
Log out to switch accounts. Normal mode keeps the user ID and password form; role restrictions apply in both modes.

On the demo dashboard, **ADC simulator** opens the simulator in a separate tab. **Switch account** returns to
the account picker. Expand **Quick demo guide** for scenarios, role guidance and the actual scaled timers;
use the Cabinets and Active alarms shortcuts to jump down the page.

The simulator button defaults to `http://127.0.0.1:5001`. For a custom simulator address, set `ADC_SIMULATOR_URL`
before starting the Monitoring System (PowerShell: `$env:ADC_SIMULATOR_URL='http://127.0.0.1:5051'`).

The login screen uses a video background; Pause background stops it. Reduced-motion preferences stop and hide
the video. Video and display fonts load from external CDNs; without internet the app uses a black background
and system fonts. No cabinet data is sent to those CDNs.

The dashboard refreshes every two seconds. Its summary shows registered cabinets, Online cabinets, active
alarms and Low inventory slots. Stale inventory is labelled. Open an ADC link to inspect slots and change a
threshold; use Reports for CSV downloads. Enable alarm tone once to hear escalation alerts.

On small screens, use Open navigation to reveal the menu. Wide tables scroll within their own region; keyboard
users can focus a table region and scroll with the arrow keys. Live updates preserve focused controls and
table scroll positions; if a control disappears, focus returns to its table. A failed refresh shows a warning
and retries automatically. Forms and navigation also work without JavaScript. Automatic polls do not extend
the session idle limit, and Fault banners update in an already-open dashboard.

The simulator groups controls into connection, alarm, inventory, request and validation scenarios. Demo seeding
and all accounts above are retained for the lab walkthrough. Generated databases, logs and Python caches are
excluded from Git.

## Tests

    python3 -W ignore -m unittest discover -s tests

374 tests. Test names include the Deliverable 1 requirement ID they check, for example
`test_FR_COM_07_...`.

## Layout

| Folder or file | Contents |
|---|---|
| `monitoring/` | Modules M01 to M14 and M17, one file each |
| `monitoring/web/` | M15 (ADC interface) and M16 (dashboard, templates, static files) |
| `adc_simulator/` | The ADC stand-in (test equipment, not part of the Monitoring System) |
| `tests/` | The test suite |
| `run_monitoring.py`, `run_simulator.py` | The two programs |

The design document maps every module to its file in section 4.2.
