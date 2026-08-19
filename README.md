# Outreach Automation

A controlled, human-in-the-loop outreach system. You choose the stage, the
batch size, and approve every send — the system handles personalization,
variant rotation, scheduling, duplicate protection, and reply/bounce
detection. Runs entirely on GitHub Actions — no local execution required
after the two one-time credential steps below.

All application code lives in a single file, **`outreach.py`**, organized
top-to-bottom in clearly labeled sections (Sheets I/O, Gmail I/O,
templating, variant rotation, eligibility, classification, CLI). Only the
config, templates, tests, README, and workflow YAML are separate files.

## How it works

- **Google Sheets** (`Master` + `Responses` tabs) is the database.
- **GitHub Actions** runs four workflows:
  - `Preview Batch` — shows exactly what would be sent, sends nothing.
  - `Send Batch` — actually sends (requires typing `SEND` to confirm).
  - `Check Replies` — runs every 30 min on a schedule, or manually.
  - `CI` — runs the test suite on every push.
- **Gmail API** sends and reads mail from your work/Workspace account.

Nothing sends automatically. You always trigger `Send Batch` yourself, for
a stage and batch size you choose, after reviewing a `Preview Batch` run.

## One-time setup

### 1. Create the Google Sheet

Create a blank Google Sheet and copy its ID from the URL
(`https://docs.google.com/spreadsheets/d/THIS_PART/edit`).

### 2. Google Sheets access (service account — fully headless)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project (or reuse one), enable the **Google Sheets API**.
2. Create a **Service Account**, then create a JSON key for it and download it.
3. Open your Sheet, click **Share**, and share it with the service account's
   `client_email` (found in the JSON) as **Editor**.
4. Save the full JSON contents as a GitHub repo secret named
   `GOOGLE_SERVICE_ACCOUNT_JSON` (Settings → Secrets and variables → Actions).

Run this once (from Cloud Shell, or any machine with Python) to create the
`Master` and `Responses` tabs with the correct headers:

```bash
pip install -r requirements.txt
GOOGLE_SERVICE_ACCOUNT_JSON='<paste the JSON>' python outreach.py setup-sheet <SHEET_ID>
```

Then set `sheet_id` in `config/campaigns.yaml` to your Sheet ID, and add
your leads as rows in the `Master` tab (set `Approval` to `Pending` — see
"Human approval" below).

### 3. Gmail API access (OAuth — one-time browser step, then headless forever)

This is the **only** step in the whole system that needs a browser, and you
only do it once.

1. In the same Cloud Console project, enable the **Gmail API**.
2. Under **OAuth consent screen**, set it up (External or Internal,
   depending on your Workspace setup); add your own address as a test user
   if prompted.
3. Under **Credentials**, create an **OAuth Client ID** of type
   **Desktop app**, and download its `client_secret.json`.
4. Run, on any machine with a browser (your laptop is fine — this is the
   one exception to "never local"):

   ```bash
   pip install -r requirements.txt
   python outreach.py generate-token client_secret.json
   ```

   A browser window opens — sign in with the mailbox you're sending from
   and approve access. It prints three values.
5. Save them as GitHub repo secrets:
   - `GMAIL_CLIENT_ID`
   - `GMAIL_CLIENT_SECRET`
   - `GMAIL_REFRESH_TOKEN`
   - `GMAIL_SENDER_ADDRESS` — your sending address, e.g. `you@yourdomain.com`

> **Workspace admin alternative:** if you're a Google Workspace admin, you
> can instead set up a service account with **domain-wide delegation**
> scoped to the Gmail API, which skips the browser step entirely. That
> requires extra setup on the Workspace admin console; the OAuth route
> above is simpler for a single mailbox and is what this repo uses by
> default.

### 4. Configure the campaign

Edit `config/campaigns.yaml` — set your real `sheet_id`, adjust stage wait
days, sending window, delay, and daily limit as needed. Templates already
exist for the `diaz_event_outreach` campaign (5 stages × 4 variants each,
in `templates/diaz_event/`) — edit the bracketed placeholders
(`[Your Name]`, `[what you do]`, etc.) before using them for real.

### 5. Push to GitHub

Push this repo to GitHub with the secrets set. The `CI` workflow runs the
test suite automatically on every push.

## Running it

All from the **Actions** tab in GitHub — no local run needed:

1. **Actions → Preview Batch → Run workflow.** Enter campaign, stage
   (`intro`, `followup1`...`followup4`), batch size. Check the job summary
   — it shows exactly who's eligible and what each email will say.
2. **Actions → Send Batch → Run workflow.** Same inputs, plus type `SEND`
   in the confirm field. This actually sends, with a randomized delay
   between each email, and updates the Master sheet row-by-row as it goes.
3. **Check Replies** runs automatically every 30 minutes. It logs every
   inbound message to the `Responses` tab and only changes a lead's status
   in `Master` when the message is a genuine reply (stops the sequence) or
   a hard bounce (stops + flags the address). Auto-replies, out-of-office
   messages, and soft bounces are logged but never stop a sequence.

## How approval works

Set `Approval` in the Master sheet to:

- `Pending` — ignored by the system entirely (default for new leads)
- `Yes` — eligible to be picked up in a batch (still requires you to
  explicitly run `Send Batch` — approval alone never triggers a send)
- `No` — permanently excluded
- `Paused` — temporarily excluded without deleting the record

## How variant rotation works

Each stage (Intro, FollowUp1–4) has 4 template variants (A/B/C/D), stored
as separate files in `templates/<campaign>/`. Rotation is **independent
per stage** — a lead's Intro variant has no bearing on their FollowUp1
variant. Within each stage, the system always assigns whichever variant
has been used least so far (counted from the Master sheet), so usage
stays balanced across your lead list over time.

You can override this for a whole batch with `--variant A` (or via the
`variant` input on the `Preview Batch` / `Send Batch` workflows, default
`Auto`) — useful when you deliberately want to test one variant rather
than the balanced auto-rotation.

## How duplicate protection works

Before any send, the eligibility check filters out anyone who already has
a timestamp in that stage's `SentAt` column — so re-running a batch can
never double-send to the same lead at the same stage.

## How thread continuation and reply matching work

**Sending:** Intro always starts a new Gmail thread. Every follow-up stage
looks up the lead's `ThreadID` (recorded from their most recent send) and
replies *within that same thread*, rather than starting a new one. This
means (a) the recipient sees one coherent conversation rather than 5
disconnected emails, and (b) `ThreadID` stays stable across a lead's whole
sequence, which is what makes matching below reliable.

**Reply matching:** when checking for replies, an inbound message is first
matched against a lead by **Gmail Thread ID** — if it lands in a thread
that belongs to one of your leads, that's treated as a certain match,
regardless of what address it came from. Only if there's no thread match
does the system fall back to matching by sender email address (the only
method in earlier versions of this system). Each logged response records
which method matched it (`MatchMethod` column in `Responses`), so you can
audit this.

This matters because email-only matching can misfire — e.g. if the same
person emails you about something unrelated, or forwards from a different
address. Thread matching is the stronger signal and is checked first.

> **Multi-campaign note:** each campaign uses its own Google Sheet, so
> leads and replies are already isolated per campaign at the data level —
> a reply logged against one campaign's sheet can't affect another
> campaign's leads. If the *same person* happens to be a lead in two
> different campaigns (two different sheets, potentially the same Gmail
> account sending from both), each campaign's `Check Replies` run only
> ever looks at its own sheet's leads, so this is handled by the existing
> per-campaign sheet separation rather than needing extra logic.

## How reply/bounce detection works

Layered checks, most reliable first:

1. Headers (`Auto-Submitted`, `X-Autoreply`, `Precedence: bulk`) → Auto-Reply
2. Sender/content-type bounce signals + SMTP status code (5xx = hard,
   4xx = soft) → Bounce (Hard) / Bounce (Soft)
3. Keyword fallback ("out of office", "undeliverable", etc.) as a last
   resort when headers are inconclusive
4. Anything left over → Genuine Reply

Every inbound message is logged to the `Responses` tab regardless of
classification, so you always have a full audit trail — but only Genuine
Reply and Bounce (Hard) change a lead's `Status` and stop the sequence
(enforced in the eligibility check itself — a replied or bounced lead is
filtered out of every future batch automatically, not just hidden in the UI).

## Batch tracking and audit trail

Every `Send Batch` run generates a `BatchID` (e.g. `BATCH-20260819-103000`)
and logs **one row per email — sent or failed — to the `SendLog` tab**,
with the lead, campaign, stage, variant, and message/thread IDs. This
answers "what did I send yesterday" or "which leads were in that batch"
without having to reconstruct it from Master.

Three tabs now exist on your Sheet:

- **Master** — current state of each lead (one row per lead)
- **Responses** — every inbound message ever detected (one row per message)
- **SendLog** — every outbound send attempt ever made (one row per send)

Master also has two fields to make its state readable at a glance without
cross-referencing multiple columns:

- **`Status`** doubles as "last action" (`Intro Sent`, `Stopped - Replied`,
  etc.) — this already existed, just documenting the intent explicitly.
- **`LastActionAt`** — timestamp companion to `Status`, updated on every
  send, reply, and bounce event.
- **`NextEligibleAt`** — computed right after each send: when this lead
  becomes eligible for the *next* stage. Blank if there's no next stage
  configured, or nothing's been sent yet.
- **`RequestedAction`** — a free-text column that is **not read by the
  system at all**. It's there purely so you can jot down intent ("send
  FU1 next") for your own bookkeeping before you go trigger the actual
  workflow. What actually gets sent is controlled entirely by the
  `Send Batch` workflow inputs, on purpose — a text note in a sheet cell
  is not something either of us wants deciding what a script sends.

## Safety limits already built in

- **Daily send limit** (`sending.daily_limit` in config) — a `Send Batch`
  run automatically caps itself if you're close to the limit.
- **Per-lead error isolation** — if sending to one lead fails, the batch
  continues to the rest rather than aborting, and the failure is logged
  to `SendLog` with the error message (the lead's `SentAt` is **not**
  written on failure, so it stays eligible for retry in the next batch).
- **Confirm-to-send gate** — the `Send Batch` workflow refuses to run
  unless you type `SEND` exactly.
- **No template execution** — variables are substituted with plain string
  replacement, never evaluated as code.

## Project structure

```
outreach.py               EVERYTHING: CLI, Sheets, Gmail, templating,
                           variant rotation, eligibility, classification
config/campaigns.yaml     per-campaign settings (includes a commented
                           second-campaign example with a shorter sequence,
                           demonstrating stages are fully config-driven)
templates/<campaign>/     20 template files (5 stages x 4 variants)
tests/test_outreach.py    unit tests
.github/workflows/        preview_batch.yml, send_batch.yml,
                           check_replies.yml, ci.yml
requirements.txt
.env.example              local reference for required env vars/secrets
```

`outreach.py` sections, in order: constants → config loading → Sheets
connector → template engine → variant selector → eligibility → Gmail
client → classifier → batch building/sending (BatchID, SendLog,
thread continuation, NextEligibleAt) → reply monitor (thread-aware
matching) → one-time setup helpers (`setup-sheet`, `generate-token`) →
main CLI commands → argument parsing.

## If you already have a Sheet from an earlier version

The Master and Responses tabs gained new columns (`RequestedAction`,
`NextEligibleAt`, `LastActionAt` on Master; `Campaign`, `MatchMethod` on
Responses), and there's a new `SendLog` tab. The header-row check is
intentionally strict — it'll raise a clear error rather than silently
writing into the wrong columns — so if you already created a sheet:

1. Add the new column headers to Master and Responses in the exact order
   shown in `outreach.py`'s `MASTER_COLUMNS` / `RESPONSES_COLUMNS`, **or**
2. Just create a fresh sheet and run `setup-sheet` again — simplest if you
   haven't sent anything for real yet.

## Running the tests locally (optional)

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

37 tests cover: reply/bounce classification, variant balancing across many
picks (including forced-variant override), eligibility rules (approval,
duplicate protection, wait periods, stopped statuses), thread continuation
logic, thread-vs-email reply matching, BatchID/NextEligibleAt generation,
a full `send_batch` integration test (success and per-lead failure paths,
against fakes — no real network calls), template rendering for all 20
templates with no leftover placeholders, and config validation.

## What's intentionally NOT in V1 (and why)

Per the original plan, still deferred: fully automatic sequencing without a
human trigger per batch, multiple email accounts, a web dashboard,
analytics, and reply categorization beyond the four buckets above. The
design stays "automation-assisted, not automation-controlled" — every send
is a decision you make.

Two specific ideas from later review were deliberately scoped down rather
than built as originally proposed:

- **A `RequestedAction` state machine that the system reads and acts on.**
  Built instead as a plain, unread free-text column (see above). A second
  field that *controls* sending would duplicate what the `Send Batch`
  workflow inputs already express, giving you two sources of truth for the
  same intent — one in a sheet cell, one in a workflow form — that could
  disagree with each other. The workflow inputs remain the single place
  that decides what actually sends.
- **Explicit "Campaign ID" propagation everywhere to prevent cross-campaign
  interference.** Not needed as a new mechanism: each campaign already has
  its own Google Sheet, so campaigns are isolated at the data level, not
  just by a shared ID column inside one sheet. `Campaign` was still added
  to the `Responses` tab for clarity when reading logs, and thread-aware
  matching (above) addresses the more specific risk this was really
  pointing at — a reply being attributed to the wrong lead.
