# Feather Auto

Local dashboard and CLI for watching Feather task availability and optionally
claiming the first task that matches a batch filter.

The main workflow is the local web dashboard. The CLI is kept as a fallback for
scripted checks and debugging.

## What It Does

- Polls Feather's task search API with your own logged-in session cookie.
- Filters tasks by active batch refs, batch suffix, batch name, or batch id.
- Supports randomized polling intervals.
- Can run in observe-only mode, open-on-found mode, or claim mode.
- Stops after a successful claim so one account does not pick up multiple tasks.
- Can open the claimed task in your existing browser for a Codex-assisted slide
  review pass.
- Keeps a local event log and status JSON for debugging.
- Provides a local dashboard with Start, Stop, campaign selection, cURL paste,
  runtime metrics, current task details, and live log tail.

## Important Limits

This project does not bypass authentication, Cloudflare, rate limits, CAPTCHAs,
or any anti-bot control. It reuses a Feather API request copied from your own
logged-in browser session.

Use it only with accounts, campaigns, and workflows where you are authorized to
work. Do not share copied cURL files, cookies, logs, or saved task JSON.

## Requirements

- Python 3.10+
- A Feather account with access to the target campaign
- Chrome or another browser that can copy a Feather API request as cURL
- Windows PowerShell examples are shown below, but the Python package itself is
  not Windows-specific

Install dependencies from the repo root:

```powershell
pip install -e .
```

or:

```powershell
pip install -r requirements.txt
```

## Quick Start

1. Open Feather in your browser while logged in.
2. Open DevTools, then the Network tab.
3. Refresh the campaign task page.
4. Right-click a request like:

```text
https://feather.openai.com/api/v2/tasks/search
```

5. Choose `Copy as cURL`.
6. Start the local dashboard:

```powershell
python -m feather_auto.dashboard_server --port 8765
```

On startup, the dashboard checks the configured Git upstream for new commits. If
the local branch is behind and the worktree is clean, it pulls the latest commit
with `git pull --ff-only` and restarts itself before serving the page. If local
changes are present, it skips the pull and starts with the current files.

7. Open:

```text
http://127.0.0.1:8765/dashboard.html
```

8. Paste the copied cURL into the dashboard, choose settings, then start the
monitor.

The dashboard saves the pasted cURL locally to:

```text
outputs/current_feather_request.curl.txt
```

That file contains authentication material. Keep it local.

## Codex-Assisted Slide Review

For slide-review tasks, the fast path is to reuse the same logged-in Feather
session and download the slide assets through Feather's GraphQL/API flow. Copy
these requests from Chrome DevTools Network once and keep them local:

- `TaskOrStagecraftRedirect`
- `FetchConversationWidget`

Then run:

```powershell
python -m feather_auto.download_task_slides `
  --api-original `
  --task-id <claimed-task-id> `
  --curl-file outputs\current_feather_request.curl.txt `
  --redirect-graphql-curl-file <TaskOrStagecraftRedirect.curl.txt> `
  --conversation-graphql-curl-file <FetchConversationWidget.curl.txt> `
  --output-dir outputs\task_slides\<claimed-task-id>
```

`--api-original` preserves the copied GraphQL operation bodies instead of using
the smaller fallback query. The downloader borrows Chrome-style headers from the
copied cURL templates and overrides only the values that must change at runtime:
cookie, referer, campaign id, and task id. It writes the raw GraphQL responses,
all slide images, `download_results.json`, and a contact sheet for visual review.

## Dashboard Modes

The dashboard exposes two main switches:

- `Claim`
- `Open task on success`

Behavior by mode:

| Claim | Open | Behavior |
| --- | --- | --- |
| Off | Off | Observe mode. Logs each new matching task as `FOUND_CONTINUING` and keeps polling. |
| Off | On | Alert mode. Opens the first matching task and stops. |
| On | Off | Claim mode. Attempts to claim matching tasks. Stops only after a successful claim. |
| On | On | Claim and open mode. Claims, verifies, opens the task on success, then stops. |

If a claim loses the race, for example Feather returns `NOT_FOUND`, the monitor
logs `CLAIM_FAILED_CONTINUING` and continues polling.

## Dashboard State Model

The dashboard server owns a background worker thread. It does not start a
separate monitor subprocess and does not depend on a monitor PID file.

Main states:

- `stopped`: no worker is active
- `starting`: worker is starting
- `monitoring`: worker is active
- `found`: a matching task was found
- `claim_failed_continuing`: claim attempt failed, monitor continues
- `claimed`: a task was successfully claimed and the worker stopped
- `error`: unrecoverable error

When the main state is `monitoring`, the `phase` field gives the lower-level
activity:

- `ready`
- `polling`
- `sleeping`

## Campaigns

The dashboard currently ships with one campaign option:

```text
929712fc-fa2a-45bc-94df-2ae6d445b2ca
```

To add more campaigns, edit `CAMPAIGNS` in:

```text
feather_auto/dashboard_server.py
```

Add entries like:

```python
CAMPAIGNS = [
    {"id": "campaign-uuid", "name": "Readable campaign name"},
]
```

The selected campaign id is sent into the same monitor logic used by the CLI.

## Batch Filtering

Common filter:

```text
-raw-creation
```

When `--batch-suffix` or the dashboard batch suffix field is set, the monitor
first loads active Feather task batch refs for the campaign and only accepts
tasks belonging to matching active batches.

Supported CLI filters:

- `--batch-suffix=-raw-creation`
- `--batch-name slides-teacher-master-spud-stage2-r4-resume240-raw-creation`
- `--batch-id 4905bfbb-d090-48be-8db8-b85267348a80`

## Tag Count Filtering

The dashboard `Tag min` and `Tag max` fields and CLI `--tag-count-min` /
`--tag-count-max` options only accept tasks whose `tags` array length falls
inside the inclusive range. Either side can be left blank to make the range
open-ended. When either filter is set, the monitor asks the Feather task search
API to include tags and applies the check before any claim attempt.

For example, `--tag-count-min 4 --tag-count-max 8` means tasks with 4 through 8
tags can be opened or claimed, while tasks outside that range are ignored.
`--tag-count 6` is still supported as shorthand for exactly 6 tags.

## CLI Usage

Dashboard usage is recommended, but the CLI remains useful for one-off tests.

Observe mode:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --curl-file my_feather_request.curl.txt
```

Observe once and exit:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --once --curl-file my_feather_request.curl.txt
```

Open the first matching task:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --open --curl-file my_feather_request.curl.txt
```

Claim the first matching task:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --claim --curl-file my_feather_request.curl.txt
```

Claim only tasks with 4 through 8 tags:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-suffix=-raw-creation --tag-count-min 4 --tag-count-max 8 --claim --curl-file my_feather_request.curl.txt
```

Use randomized polling intervals:

```powershell
python -u -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --interval-min 1.2 --interval-max 3.8 --batch-suffix=-raw-creation --claim --curl-file my_feather_request.curl.txt
```

Write live logs and status JSON:

```powershell
python -u -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --interval-min 1.2 --interval-max 3.8 --batch-suffix=-raw-creation --claim --curl-file my_feather_request.curl.txt --log-file outputs/feather-auto.log --status-file outputs/feather-auto-status.json
```

Watch the log:

```powershell
Get-Content .\outputs\feather-auto.log -Wait -Tail 80
```

## Claim Verification

When claim mode is enabled, the monitor:

1. Sends the GraphQL `UpdateTaskStatus` mutation with `status=IN_PROGRESS`.
2. Queries `whoami`.
3. Searches for the task again.
4. Prints a `VERIFY` line with expected user id/email, assignment fields, and
   workflow status.

The monitor treats the claim as successful when Feather confirms the task is in
progress for the current user. On success it prints:

```text
CLAIM_SUCCEEDED_STOPPING <task_id>
```

and exits the worker.

## Runtime Files

Runtime artifacts are intentionally local-only and ignored by Git:

```text
outputs/current_feather_request.curl.txt
outputs/raw_creation_claim_monitor.log
outputs/raw_creation_claim_status.json
outputs/last_claimed_raw_creation_task.json
outputs/dashboard_server.pid
```

Do not commit or share these files.

## Troubleshooting

### Dashboard does not open

Make sure the server is running:

```powershell
python -m feather_auto.dashboard_server --port 8765
```

Then open:

```text
http://127.0.0.1:8765/dashboard.html
```

### Stop does not immediately stop

Stop is cooperative. The worker checks the stop event before polling and during
sleep. A request already in flight may finish before the worker stops.

### 401 or 403

Your copied cURL/cookie is expired or no longer accepted. Copy a fresh Feather
API request from your logged-in browser, paste it into the dashboard, save it,
and restart the monitor.

### Found tasks repeat in the log

Within one worker run, task ids are tracked in memory and should not repeat as
new `FOUND` events. Restarting the worker resets that memory.

### Dashboard says `monitoring` but phase is `sleeping`

That is expected. `monitoring` is the main state. `sleeping` means the worker is
waiting for the next randomized poll interval.

## Development Notes

- `feather_auto/cli.py` contains the shared monitor logic.
- `feather_auto/dashboard_server.py` serves the local dashboard and owns the
  in-process worker thread.
- `dashboard.html` is a plain HTML/CSS/JS dashboard with no frontend build step.
- Default minimum poll interval is 1 second. Lower values are rejected.

Before pushing changes:

```powershell
python -m py_compile .\feather_auto\cli.py .\feather_auto\dashboard_server.py
git diff --check
```
