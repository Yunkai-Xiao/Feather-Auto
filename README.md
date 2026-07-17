# Feather Auto

Local dashboard and CLI for watching Feather task availability and optionally
claiming the first task that matches a batch filter.

The main workflow is the local web dashboard. The CLI is kept as a fallback for
scripted checks and debugging.

## What It Does

- Polls Feather's task search API with your own logged-in session cookie.
- Filters tasks by active batch refs, batch regex, batch name, or batch id.
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

## One-Click Windows Setup

On a fresh Windows computer, copy or clone this repo, then double-click this
file in the repo root:

```text
setup.cmd
```

That is the normal setup path. It installs what is missing, creates the local
Python environment, starts the dashboard, and opens:

```text
http://127.0.0.1:8001/dashboard.html
```

The setup script:

- Installs Python 3.12 with `winget` if Python 3.10+ is not already available.
- Creates `.venv` in the repo.
- Installs the package and dependencies with `pip install -e .`.
- Adds `feather` and `Start-Feather` commands to your PowerShell profile.
- Starts the dashboard.

After setup, start the dashboard again by double-clicking `start-feather.cmd`.
From PowerShell, run:

```powershell
.\start-feather.cmd
```

Or open a new PowerShell and run:

```powershell
feather
```

If you do not want setup to edit your PowerShell profile or start the dashboard:

```powershell
.\setup.cmd -NoProfileCommand -NoStart
```

Manual dependency install, only if you are not using `setup.cmd`:

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
6. Start the local dashboard if it is not already open:

```powershell
.\start-feather.cmd
```

The direct PowerShell equivalent is `feather` after setup, or
`python -m feather_auto.dashboard_server --port 8001` from an activated
environment.

On startup, the dashboard checks the configured Git upstream for new commits. If
the local branch is behind and the worktree is clean, it pulls the latest commit
with `git pull --ff-only` and restarts itself before serving the page. If local
changes are present, it skips the pull and starts with the current files.

7. Open:

```text
http://127.0.0.1:8001/dashboard.html
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

To run the full Content Grading helper after a task is claimed, sign in to
Codex with your ChatGPT subscription or set `OPENAI_API_KEY`, then run:

```powershell
python -m feather_auto.review_task_slides `
  --task-id <claimed-task-id> `
  --curl-file outputs\current_feather_request.curl.txt `
  --redirect-graphql-curl-file outputs\current_feather_task_or_stagecraft_redirect.curl.txt `
  --conversation-graphql-curl-file outputs\current_feather_conversation_widget.curl.txt
```

The review pipeline downloads the slide images, extracts visible slide
sentences and phrases with local PaddleOCR by default, flags vague or
AI-slop-like content, and writes reviewer notes with critique plus improvement
suggestions:

```text
outputs/content_review/<task-id>/slide_text_by_deck.json
outputs/content_review/<task-id>/content_issue_candidates.json
outputs/content_review/<task-id>/content_grading_comments.json
outputs/content_review/<task-id>/content_grading_comments.md
```

By default, slide OCR uses local PaddleOCR with the fast PP-OCRv5 mobile models.
The setup script installs both `paddleocr` and the PaddlePaddle CPU runtime.
The dashboard auto-review path always uses PaddleOCR for slide text extraction.
Non-Paddle OCR is treated as an explicit debug fallback only:

```powershell
# Local OCR, default
$env:FEATHER_REVIEW_OCR_BACKEND = "paddle"

# Explicit debug fallback only
$env:FEATHER_REVIEW_ALLOW_NON_PADDLE_OCR = "1"
$env:FEATHER_REVIEW_OCR_BACKEND = "codex"
```

By default, `--llm-backend auto` uses `OPENAI_API_KEY` when it is present. If no
API key is present and the `codex` CLI is available, it runs `codex exec` with
your Codex/ChatGPT subscription auth instead:

```powershell
python -m feather_auto.review_task_slides `
  --llm-backend codex `
  --codex-model gpt-5.5 `
  --task-id <claimed-task-id>
```

The Codex backend defaults to `--review-speed fast`: it OCRs slides locally with
PaddleOCR, starts a deck-level Codex comment as soon as each deck's OCR is
ready, continues OCR on later decks while Codex writes earlier decks, then runs a
final cross-deck ranking. This keeps Codex calls to roughly one per deck plus
one ranking pass. Use `--review-speed thorough` only when you also want
per-slide Codex drafts; that mode is much slower. Set `--codex-workers N` or
`FEATHER_REVIEW_CODEX_WORKERS=N` to control parallel deck comment workers. Set
`--ocr-workers N` or `FEATHER_REVIEW_OCR_WORKERS=N` to control parallel
PaddleOCR workers in fast mode. Codex workers default to `3`; OCR workers
default to `4`. Fast mode requests
six comments per deck by default; override it with `--comments-per-deck N` or
`FEATHER_REVIEW_COMMENTS_PER_DECK=N`.

Review output is streamed to disk as it progresses. Per-deck comment drafts are
written under:

```text
outputs/content_review/<task-id>/deck_reviews/
```

The combined `content_grading_comments.md` is refreshed as each deck finishes,
then receives a final cross-deck quality ranking from strongest to weakest.

The dashboard's `Auto review after claim` switch runs the same pipeline after a
successful claim. It only writes local helper files; it does not submit or edit
Feather comments.

The dashboard has two workflow presets:

- `Aesthetic Ranking`: uses the known ranking campaign, keeps `Aesthetic`
  as the default batch regex, sets Tag max to `8`, and leaves auto review off
  by default.
- `Content Grading`: uses the known content-grading campaign, leaves batch
  regex blank so ranking-specific filters do not hide tasks, and turns auto
  review on by default.

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

Before claim mode starts and immediately before each claim attempt, the monitor
checks whether the current account already has an `in_progress` task in the
campaign. If one exists, it stops without claiming and shows a blocking alert.

When `Keep running in background` is off, the worker requires a live monitor
session heartbeat. If that heartbeat is lost for about 20 seconds, the server
stops search and claim activity.

`Close backend when dashboard closes` is enabled by default. The last open
Dashboard page starts a five-second shutdown grace period, which allows an
ordinary refresh to reconnect without stopping the service. Multiple Dashboard
tabs are tracked independently. If the browser cannot send its close notice, a
90-second page-lease timeout provides a fallback. Turn the switch off to keep
the service on port 8001 after closing the page, or use `Close backend` in the
top bar to stop the worker and server immediately.

## Dashboard State Model

The dashboard server owns a background worker thread. It does not start a
separate monitor subprocess and does not depend on a monitor PID file.

Main states:

- `stopped`: no worker is active
- `starting`: worker is starting
- `monitoring`: worker is active
- `found`: a matching task was found
- `blocked_in_progress`: claim mode stopped because this account already has an
  in-progress task
- `claim_failed_continuing`: claim attempt failed, monitor continues
- `claimed`: a task was successfully claimed and the worker stopped
- `stopped_inactive`: the dashboard session heartbeat was lost, so the worker
  stopped before continuing background search or claim
- `error`: unrecoverable error

When the main state is `monitoring`, the `phase` field gives the lower-level
activity:

- `ready`
- `polling`
- `sleeping`

## Campaigns

The dashboard currently ships with fixed campaign options:

```text
Aesthetic Ranking: 929712fc-fa2a-45bc-94df-2ae6d445b2ca
Content Grading: c2978c67-7bc5-4fde-b4f5-330d0e001a35
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

For a new campaign, add it to both `CAMPAIGNS` and `REVIEW_MODES` in
`feather_auto/dashboard_server.py`.

The selected campaign id is sent into the same monitor logic used by the CLI.

## Batch Filtering

Common filter:

```text
Aesthetic
```

When `--batch-regex` or the dashboard batch regex field is set, the monitor
first loads active Feather task batch refs for the campaign and only accepts
tasks belonging to matching active batches.

Supported CLI filters:

- `--batch-regex="Aesthetic"`
- `--batch-name slides-teacher-master-spud-stage2-r4-resume240-raw-creation`
- `--batch-id 4905bfbb-d090-48be-8db8-b85267348a80`

`--batch-suffix=-raw-creation` is still accepted as a backwards-compatible
alias for `--batch-regex="\-raw\-creation$"`.

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
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-regex="Aesthetic" --curl-file my_feather_request.curl.txt
```

Observe once and exit:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-regex="Aesthetic" --once --curl-file my_feather_request.curl.txt
```

Open the first matching task:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-regex="Aesthetic" --open --curl-file my_feather_request.curl.txt
```

Claim the first matching task:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-regex="Aesthetic" --claim --curl-file my_feather_request.curl.txt
```

Claim only tasks with 4 through 8 tags:

```powershell
python -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --batch-regex="Aesthetic" --tag-count-min 4 --tag-count-max 8 --claim --curl-file my_feather_request.curl.txt
```

Use randomized target polling periods (request time is subtracted, so a slow request does not add a second full sleep):

```powershell
python -u -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --interval-min 0.25 --interval-max 0.75 --batch-regex="Aesthetic" --claim --curl-file my_feather_request.curl.txt
```

Write live logs and status JSON:

```powershell
python -u -m feather_auto.cli --campaign-id 929712fc-fa2a-45bc-94df-2ae6d445b2ca --interval-min 0.25 --interval-max 0.75 --batch-regex="Aesthetic" --claim --curl-file my_feather_request.curl.txt --log-file outputs/feather-auto.log --status-file outputs/feather-auto-status.json
```

Watch the log:

```powershell
Get-Content .\outputs\feather-auto.log -Wait -Tail 80
```

## Claim Verification

When claim mode is enabled, the monitor:

1. Queries `whoami` and checks once at startup for an existing `in_progress` task.
2. Polls a global fast lane and all matching batches concurrently over persistent HTTP connections.
3. Dispatches the GraphQL `UpdateTaskStatus` mutation immediately when any response
   contains an eligible task, before log, status, or artifact writes.
4. Trusts a definitive GraphQL success or error response. It only performs the slower
   follow-up assignment search when the mutation response is ambiguous.

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
python -m feather_auto.dashboard_server --port 8001
```

Then open:

```text
http://127.0.0.1:8001/dashboard.html
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
