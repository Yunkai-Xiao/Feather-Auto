# Feather Auto

Small Python CLI for monitoring Feather task availability and optionally claiming
the first task that matches a batch filter.

This tool does not bypass authentication or anti-bot checks. It uses a Feather
session cookie copied from your own logged-in Chrome session. Use it only for
accounts and campaigns where you have permission to work.

## Requirements

- Python 3.10+
- A Feather account with access to the target campaign
- A Feather request copied from Chrome DevTools as cURL, saved to a local file

Install dependencies:

```powershell
pip install -e .
```

or:

```powershell
pip install -r requirements.txt
```

## Getting Your Cookie

1. Open Feather in Chrome while logged in.
2. Open DevTools -> Network.
3. Refresh the campaign or task page.
4. Right-click any Feather API request, such as:

```text
https://feather.openai.com/api/v2/tasks/search
```

5. Choose "Copy as cURL".
6. Save it locally, for example:

```text
my_feather_request.curl.txt
```

Do not share this file. It contains your login cookie.

## Usage

Monitor only, do not claim:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --curl-file my_feather_request.curl.txt
```

When claim and open-on-success are both disabled, found tasks are logged with
`FOUND_CONTINUING` and the monitor keeps polling for later new tasks.

Monitor and claim the first matching task:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --claim --curl-file my_feather_request.curl.txt
```

If a claim attempt loses the race, for example Feather returns `NOT_FOUND`, the
CLI logs `CLAIM_FAILED_CONTINUING` and keeps polling. It stops only after a
successful claim or an unrecoverable error such as expired/invalid auth.

Use a random polling interval:

```powershell
python -u -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --interval-min 1.2 --interval-max 3.8 --batch-suffix=-raw-creation --claim --curl-file my_feather_request.curl.txt
```

To keep a live log file:

```powershell
python -u -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --interval-min 1.2 --interval-max 3.8 --batch-suffix=-raw-creation --claim --curl-file my_feather_request.curl.txt --log-file outputs/feather-auto.log --status-file outputs/feather-auto-status.json
```

Watch it in another PowerShell window:

```powershell
Get-Content .\feather-auto.log -Wait -Tail 80
```

## Dashboard

The repo includes a small static dashboard at `dashboard.html`. It expects these
default runtime files:

```text
outputs/raw_creation_claim_monitor.log
outputs/raw_creation_claim_status.json
outputs/current_feather_request.curl.txt
```

Start the local dashboard server from the repo root:

```powershell
python -m feather_auto.dashboard_server --port 8765
```

Then open:

```text
http://127.0.0.1:8765/dashboard.html
```

The dashboard can:

- Save a pasted Feather cURL locally.
- Start or stop the in-process monitor worker.
- Configure campaign id, batch suffix, random interval min/max, claim mode, and open-on-success.
- Show current status JSON and live log tail.

The dashboard no longer starts a separate CLI subprocess for monitoring. The
server owns a background worker thread and stops it with an in-memory stop
event, so it does not depend on a stale `raw_creation_claim_monitor.pid` file.

Run one check and exit:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --once --curl-file my_feather_request.curl.txt
```

Open the found task in the browser:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --open --curl-file my_feather_request.curl.txt
```

## Filters

You can combine these filters:

- `--batch-suffix=-raw-creation`
- `--batch-name slides-teacher-master-spud-stage2-r4-resume240-raw-creation`
- `--batch-id 4905bfbb-d090-48be-8db8-b85267348a80`

When `--batch-suffix` is used, the CLI first loads active task batch refs and
matches against both batch id and batch name.

## Claim Verification

When `--claim` is enabled, the CLI:

1. Sends the GraphQL `UpdateTaskStatus` mutation with `status=IN_PROGRESS`.
2. Queries `whoami`.
3. Searches the task again and prints a `VERIFY` line containing:
   - expected user id/email
   - claimed user id/email
   - active user id/email
   - workflow status

Use the `VERIFY` line to confirm that the task is actually assigned to the
intended account.

## Notes

- Default minimum interval is 1 second. Lower values are refused.
- Cookies expire. If requests start returning 401/403, copy a fresh cURL from
  Chrome and restart the CLI.
- Avoid committing cURL files, cookies, logs, or saved task JSON.
