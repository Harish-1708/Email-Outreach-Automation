# Email Outreach Automation — Complete System Documentation

A full cold-email outreach platform: multi-stage campaigns, real SMTP/IMAP
sending and reply detection, a Google Sheet as the single source of truth,
GitHub Actions as the execution engine, and a Streamlit control panel for
everyday work — no dedicated server, no per-seat SaaS subscription, and
every credential scoped to exactly what actually needs it.

This document explains how the whole system fits together, what each
piece is built with and why, and what you actually get from it end to
end — from adding a lead to a campaign, through every follow-up, to
reading and replying to what comes back.

---

## Table of contents

1. [What this system does](#1-what-this-system-does)
2. [Architecture at a glance](#2-architecture-at-a-glance)
3. [The five things that hold the whole system together](#3-the-five-things-that-hold-the-whole-system-together)
4. [End-to-end walkthrough](#4-end-to-end-walkthrough)
5. [Every workflow, in detail](#5-every-workflow-in-detail)
6. [The Streamlit control panel, page by page](#6-the-streamlit-control-panel-page-by-page)
7. [The Google Sheet, tab by tab](#7-the-google-sheet-tab-by-tab)
8. [Security model](#8-security-model)
9. [Safety features built into the sending logic](#9-safety-features-built-into-the-sending-logic)
10. [Reliability features](#10-reliability-features)
11. [Testing](#11-testing)
12. [Technology stack](#12-technology-stack)
13. [What this gets you, concretely](#13-what-this-gets-you-concretely)
14. [Known limitations](#14-known-limitations-by-design-not-oversights)
15. [One-time setup checklist](#15-one-time-setup-checklist)

---

## 1. What this system does

Run real, multi-stage cold email campaigns — an intro plus up to four
follow-ups, each with multiple A/B/C/D variants — across as many sender
accounts as you need (Gmail or any SMTP/IMAP provider, like Hostinger),
with:

- Automatic sending on a schedule, with daily limits, per-account limits,
  and sender rotation
- Automatic detection of replies, bounces, auto-replies, and
  out-of-office messages, with the sequence stopping the instant a real
  reply or a hard bounce comes in
- Optional AI classification of what a reply actually *means*
  (Interested / Not Interested / Lead-Needs-Follow-up / Unclear) — the
  business intent, not just "did they reply"
- A unified inbox-style Responses page across every campaign at once,
  with real conversation threads, search, filters, and reply-from-app
- A full campaign management surface: create, launch, pause, resume,
  temporarily remove (fully reversible), or permanently delete a
  campaign, its stages, or its variants
- Email account management — add, edit, remove, or bulk-import sender
  accounts directly from the app, across Gmail and third-party providers

Every one of these is real, tested, and already built and running — none
of this is a mockup.

---

## 2. Architecture at a glance

```
                     ┌─────────────────────────┐
                     │   Streamlit Control      │
                     │   Panel (the app you     │
                     │   actually use daily)    │
                     └────────────┬─────────────┘
                                  │
              reads (read-only)  │  commits files + triggers workflows
                                  │  (never sends email, never has
                                  │   SMTP/IMAP passwords)
                                  ▼
                     ┌─────────────────────────┐
                     │   GitHub repository       │
                     │   - outreach.py (engine)  │
                     │   - templates/            │
                     │   - config/               │
                     │   - .github/workflows/    │
                     └────────────┬─────────────┘
                                  │ triggers
                                  ▼
                     ┌─────────────────────────┐
                     │   GitHub Actions          │
                     │   (the only thing that    │
                     │   ever touches real       │
                     │   credentials)            │
                     └──────┬─────────────┬──────┘
                            │             │
                 SMTP/IMAP  │             │  Google Sheets API
                 (send /    │             │  (leads, sends, replies,
                  check)    ▼             ▼   errors, dashboards)
                     ┌─────────────┐ ┌─────────────────┐
                     │  Email       │ │  Google Sheet     │
                     │  Provider(s) │ │  (source of truth) │
                     │  Gmail /     │ └─────────────────┘
                     │  Hostinger / │
                     │  any SMTP+IMAP│
                     └─────────────┘
```

**The core idea**: Streamlit is the *interface*, not the *engine*. It
never sends an email, never holds an SMTP password, and never writes to
the Sheet directly except through a narrow set of explicitly-permitted
paths. Every consequential action — sending a batch, checking for
replies, adding a lead, adding an email account — goes through a GitHub
Actions workflow that runs `outreach.py`, the one place all the real
business logic lives. This keeps the surface that can be attacked or
misused small and auditable: if you can read the GitHub Actions history,
you can see exactly what happened and when.

## 3. The five things that hold the whole system together

### `outreach.py` — the engine
A single, dependency-light Python script (~3,100 lines) that contains
every piece of real business logic in the system: rendering templates,
deciding who's eligible to be sent to today, sending email over SMTP,
fetching and classifying replies over IMAP, computing dashboard numbers,
and more. It's a plain command-line tool — `python outreach.py <command>
--campaign <name> ...` — runnable identically from a laptop or from a
GitHub Actions runner. Nothing about it depends on Streamlit or GitHub
Actions being involved at all; those are just two different ways of
invoking it.

### The Google Sheet — the source of truth
Every lead, every email actually sent, every reply received, every
error, and every account's live connection status lives in one shared
Google Sheet, organized into tabs per campaign (see
[section 7](#7-the-google-sheet-tab-by-tab)). This is deliberately *not*
a custom database: a Sheet is human-readable, exportable, easy to audit
by eye, and something a non-technical person can open and understand
without touching the app at all.

### GitHub Actions — the execution engine
Every workflow (`.github/workflows/*.yml`) is a small wrapper that
checks out the repo, installs dependencies, and runs one `outreach.py`
command with the real credentials as environment variables. Workflows
trigger two ways: **on a schedule** (checking for replies every 10
minutes, checking account health every 2 hours, refreshing dashboards
daily) or **on demand**, dispatched by a click in Streamlit (sending a
batch, adding leads, replying to someone, adding an email account).

### GitHub Secrets — where every real credential actually lives
The Google Sheets service account key, every email account's SMTP/IMAP
password, and (optionally) the Anthropic API key all live as encrypted
GitHub repository secrets, injected into a workflow's environment only
for the duration of that one run. Streamlit never sees any of them.

### The Streamlit app — the everyday interface
A multi-page app (`streamlit_app/`) that reads the Sheet (via a
Viewer-only Google credential) to show you what's happening, and writes
changes by either committing a file directly to the repo (for things
like campaign settings or template edits) or committing a small JSON
payload and then triggering the right GitHub Actions workflow to act on
it (for anything that needs real credentials, like sending). See
[section 6](#6-the-streamlit-control-panel-page-by-page) for the full
page-by-page breakdown.

---

## 4. End-to-end walkthrough

This is the actual life cycle of one lead, from being added to a
campaign through however many follow-ups it takes to get a reply,
narrated step by step with exactly what's happening underneath.

### Step 1 — A campaign exists

A campaign is nothing more than a folder of template files:
`templates/<CampaignName>/intro_A.txt`, optionally
`followup1_A.txt` through `followup4_A.txt`, and optionally more
variants (`_B.txt`, `_C.txt`, `_D.txt`) of each stage. Creating a
campaign in Streamlit (Campaigns → "➕ New Campaign") just writes these
files and commits them straight to `main` — there's no database record
of "campaigns" anywhere; `outreach.py` discovers which campaigns exist
by scanning this folder. A campaign's optional settings (sending limits,
schedule, sender accounts, status) live in a separate, equally simple
file: `config/campaigns/<CampaignName>.yaml`.

### Step 2 — Leads get added

Via the Data tab's CSV upload (or the `import-leads` CLI command
directly), a spreadsheet of names and emails becomes rows in that
campaign's Master Sheet tab. Every lead starts with `Approval: Pending`
— **nothing sends to anyone until you explicitly approve them**, whether
that's one at a time in the Data tab or in bulk.

### Step 3 — Sending the Intro

From Settings (only visible while a campaign is actually Running), you
pick a stage and a variant, type `SEND` to confirm, and click Send Batch.
This commits nothing — it directly triggers the `send_batch.yml`
workflow with those parameters. Inside that workflow, `outreach.py`:

1. Loads every lead, filters to ones that are `Approval: Yes`, haven't
   already been sent this stage, and are eligible under the campaign's
   schedule (day-of-week and time-of-day window, timezone-aware).
2. Picks a sending account — either the one you configured, or rotated
   across several if `sender_rotation` is on — respecting each account's
   daily send limit.
3. Renders the template for each lead, substituting `{{FirstName}}`,
   `{{CompanyName}}`, and so on with that lead's actual data.
4. Sends over real SMTP (Gmail's, or a third-party provider's own host
   and port if configured), generating a real `Message-ID` and setting
   proper `Date`/`From`/`To` headers.
5. Logs the result to the Send Log tab, and updates that lead's row in
   the Master Sheet with `IntroSentAt`, `IntroVariant`, the message ID,
   and the beginning of a `ThreadReferences` chain used to keep every
   future email in the same conversation thread.

### Step 4 — Follow-ups

The same Send flow, but for `followup1` through `followup4`. Each stage
has its own configurable wait period after the previous one
(`wait_days_after_previous`), and **every stage must use the same
variant a lead started with** — if someone got variant B of the Intro,
they get variant B of every follow-up too, so the conversation reads
consistently. Every follow-up continues the exact same email thread as
the Intro, using `In-Reply-To` and `References` headers, so it lands in
the recipient's inbox as one thread, not four separate emails.

### Step 5 — They reply (or bounce, or auto-reply)

Every 10 minutes (or on demand via "Check Replies Now"), the
`check_replies.yml` workflow logs into every configured account's IMAP,
fetches anything new, and for each message:

1. **Classifies it mechanically** — Genuine Reply, Auto-Reply, Out of
   Office, Bounce (Hard), or Bounce (Soft) — based on message headers
   and content patterns (delivery-status reports, `Auto-Submitted`
   headers, out-of-office keyword patterns, and so on).
2. **Matches it to a lead** — first by checking whether this message's
   `In-Reply-To`/`References` headers actually reference a
   `Message-ID` this system itself sent (a *verified* match, safe to
   act on), and only if that fails, falls back to matching by sender
   email address alone (an *unverified* match — logged for visibility,
   but never allowed to stop a sequence, since two different people
   could share an inbox or an email could be spoofed).
3. **Stops the sequence** if it's a Genuine Reply or a Hard Bounce
   (verified match only) — no further follow-ups will ever go to this
   lead.
4. **Logs the full message** to the Response Sheet — not just a short
   preview, but the complete text, so a later conversation view can
   show exactly what was said without needing to fetch it again.
5. **Optionally classifies sales intent** — if you've configured an
   Anthropic API key, every Genuine Reply gets a second, independent
   classification: Interested, Not Interested, Lead-Needs-Follow-up, or
   Unclear, with a confidence level. A low-confidence result always
   shows as Unclear rather than risking a wrong guess on something that
   could affect a real business decision. This only ever happens once
   per message, and never for a bounce or auto-reply.

### Step 6 — You read it and reply

The Responses page shows every reply across every campaign, filterable
by that classification and intent, by campaign, or to unread only, with
free-text search. Clicking "💬 View full conversation" reconstructs the
entire thread — every stage that was actually sent (re-rendered live
from the real template, so it can never drift from what truly went out)
interleaved chronologically with every reply — without needing to fetch
anything live. Replying sends a real email, through the same threading
headers, Cc/Bcc, and optional file attachments, via the same
commit-and-trigger pattern as sending a batch — Streamlit never touches
the SMTP password to do this.

---

## 5. Every workflow, in detail

| Workflow | Trigger | What it does | Credentials it needs |
|---|---|---|---|
| `send_batch.yml` | Manual (from Settings) | Sends one batch for one stage/variant of one campaign | Google Sheets (read-write), the sending account(s) |
| `check_replies.yml` | Every 10 min, or manual | Fetches new IMAP mail across every account, classifies, matches, logs, optionally runs intent classification | Google Sheets, every account's IMAP credentials, optionally the Anthropic key |
| `send_reply.yml` | Manual (from Responses page) | Sends one manual reply, correctly threaded | Google Sheets, the specific sending account |
| `import_leads.yml` | Manual (from Data tab) | Adds a batch of leads from an uploaded CSV | Google Sheets |
| `remove_leads.yml` | Manual (from Data tab) | Soft-removes leads (marks `Status: Removed`, never deletes the row) | Google Sheets |
| `mark_responses_read.yml` | Manual (batched, from Responses page) | Marks a batch of responses as read, persistently | Google Sheets only |
| `check_account_health.yml` | Every 2 hours, or manual | Logs into every account via IMAP (connectivity check only) and records Connected/Disconnected | Every account's IMAP credentials |
| `dashboard.yml` | Daily, or manual | Recomputes and writes the Dashboard tab's numbers | Google Sheets |
| `backfill_thread_subject.yml` | Manual (Maintenance, one-off) | Fixes `ThreadSubject` for leads that predate that field existing | Google Sheets |
| `preview_batch.yml` | Manual, from GitHub's own Actions tab | A CLI-equivalent preview, for running one outside Streamlit | Google Sheets (read-only preview, never sends) |
| `ci.yml` | Every push/PR | Runs the full automated test suite | None (no real credentials involved) |

A few things worth calling out about how these are built:

- **Every workflow that commits a "processed" cleanup back to the repo
  uses `git rm` on the exact file, never a blanket `git add -A`** — an
  earlier version of this swept up unrelated leftover files into the
  same commit; fixed once, applied everywhere the same pattern occurs.
- **Every workflow that needs to push a commit declares
  `permissions: contents: write` explicitly**, rather than relying on
  whatever the repository's default token permission happens to be —
  this was a real bug (a 403 error on an otherwise-successful run) found
  and fixed across every affected workflow at once.
- **Every Google Sheets API call automatically retries** on a transient
  429 (rate limit) or 5xx (Google's own service hiccup) with exponential
  backoff, but never retries a genuine permissions error — that fails
  immediately rather than wasting time.

---

## 6. The Streamlit control panel, page by page

### 🗂️ Campaigns
The everyday page — the only one you need for day-to-day work. A
searchable list of every campaign with its live status (Draft / Running
/ Paused / Completed / Attention needed / Deleted), a "🗑️ Deleted
Campaigns" section for anything temporarily removed, and a
"➕ New Campaign" dialog. Opening a campaign gives you:

- **Analytics** — sent/opened/replied numbers, broken down by stage,
  variant, and sender account, plus recent errors
- **Data** — upload a CSV of leads (with an in-app example of the exact
  columns expected), approve or remove leads, edit fields inline
- **Sequences** — edit each stage/variant's subject and body, with a
  live preview using a real lead's data, add a new variant (campaign-wide,
  so every stage stays in sync), delete a variant or the last stage
  (never a middle one — see [section 9](#9-safety-features-built-into-the-sending-logic))
- **Schedule** — which days and hours this campaign is allowed to send,
  in a real IANA timezone (correctly DST-aware)
- **Settings** — daily limits, per-account limits, sender rotation, the
  actual Send action (only available while Running), and the Danger
  Zone (Temporarily Remove / Permanently Delete)
- **Responses** — every reply for this specific campaign, with
  Check Replies Now and reply-from-app

### 💬 Responses
The cross-campaign inbox. Every reply from every campaign in one place,
filterable by sales intent or mechanical classification, by campaign, or
to unread only, plus free-text search across sender/subject/body/
campaign. Each response can be expanded into its full reconstructed
conversation thread, or replied to directly. Marking something read is
explicit (a "✓ Mark as read" button, or sending a reply) and persists
permanently once synced — never just a per-session illusion.

### 📈 Overview
Every campaign at a glance in one table: total leads, sent, replies,
reply rate, completion — the fastest way to see how everything is doing
without opening any one campaign.

### 📊 Dashboard
A read-only, deep per-campaign view, using the exact same computation
`outreach.py` itself uses for the Sheet's own Dashboard tab, so the two
always agree with each other.

### 📧 Email Accounts
Every sender account, how much each has sent today, and its live
connection status (🟢 Connected / 🔴 Disconnected with the actual reason
/ ⚪ Unknown before the first check). Add, edit, or remove an account
right here — one at a time, or in bulk via CSV — for Gmail or any
custom SMTP/IMAP provider, with the password only ever passing through
the app's memory for the instant it takes to encrypt and send it to
GitHub.

---

## 7. The Google Sheet, tab by tab

Each campaign gets its own set of tabs, named `<CampaignName> <Tab>`:

- **Master Sheet** — one row per lead: contact info, `Approval` status,
  which account is sending to them, and for every stage, when it was
  sent and which variant. This is the single record of "where is this
  lead in the sequence."
- **Response Sheet** — one row per inbound message: sender, subject, the
  full body text, mechanical classification, sales intent (if
  configured), whether it's been read, and what action it triggered
  (stopped the sequence or just logged).
- **Custom Log Sheet** (Send Log) — one row per actual send attempt,
  success or failure, with the account used and the resulting
  `Message-ID`.
- **Error Log** — anything that went wrong (a bad template variable, a
  Sheets API hiccup, an account hitting its daily cap), so failures are
  visible without digging through Actions logs.
- **Dashboard** tab (per campaign) and **All Campaigns Dashboard** (one,
  shared) — precomputed summary numbers, refreshed daily.

Plus two tabs shared across every campaign:

- **Email Accounts Health** — the live Connected/Disconnected status
  for every configured account.
- (Non-secret) **slot mapping file** in the repo, not the Sheet —
  `config/email_account_slots.yaml` tracks which GitHub Secret slot each
  account occupies, containing only names, addresses, and slot numbers,
  never a password.

A Sheet's header row can gain new columns over time (this has happened
several times as features were added) without ever breaking an existing
campaign — the system detects an existing header that's a valid prefix
of what's newly required and safely widens it, rather than erroring.

---

## 8. Security model

**Streamlit never holds a real credential that can send email, receive
email, or write to the Sheet.** Concretely:

- Streamlit's Google Sheets access uses a **Viewer-only** service
  account key — it can read every tab, but a Google-side permission
  denies it write access even if the code tried.
- Every SMTP/IMAP password lives only in GitHub Secrets, injected into
  a workflow's environment for the duration of one run, and never
  logged, displayed, or stored by Streamlit — even the "Add Account"
  form only holds a password in memory for the instant it takes to
  encrypt it and hand it to GitHub's API.
- **GitHub Secrets are write-only by design** — no token, not even one
  with full admin access, can ever read an existing secret's value back.
  This is why accounts are managed one-secret-per-account rather than
  one shared JSON blob: editing one account never requires knowing (or
  reconstructing) any other account's password.
- Bcc recipients are added to the SMTP envelope but **never** written
  into the message's own headers — verified by a test that would fail
  immediately if a future change accidentally made Bcc visible to every
  other recipient.
- The GitHub token Streamlit uses is scoped to exactly three
  permissions, no more: `Actions: read/write` (to trigger and poll
  workflows), `Contents: read/write` (to commit files), and optionally
  `Secrets: write` (only if you want in-app account management — a
  materially bigger grant, requiring your own explicit decision to add
  it, since a token with this permission could overwrite your sending
  accounts' credentials if it were ever compromised — though even then,
  it still could never read an existing one back).

---

## 9. Safety features built into the sending logic

- **Nothing sends without explicit approval.** Every lead defaults to
  `Approval: Pending` — including everyone in a freshly-uploaded CSV,
  even if that CSV had its own "Approval" column you didn't map.
- **A Draft or Paused campaign cannot send**, enforced at the actual
  sending function itself (not just hidden in the UI) — so even a
  workflow triggered directly, bypassing Streamlit entirely, is still
  blocked.
- **Typed confirmation for the two genuinely destructive actions**:
  sending a real batch requires typing `SEND` exactly; permanently
  deleting a campaign requires typing that campaign's exact name.
- **Deleting is never actually deleting**, in two different ways:
  - Removing a lead sets `Status: Removed` — the row stays, visible and
    recoverable, forever.
  - "Temporarily Remove" on a whole campaign just changes its status —
    every template, every setting, every lead and every reply stays
    completely untouched, and Restore brings it back to *exactly* the
    state it was in before removal (Running stays Running, Paused stays
    Paused — never silently reset to Draft).
  - Even "Permanently Delete" only ever removes template and config
    files — the Google Sheet's data (every lead, every send, every
    reply) is never touched by any delete action in this system.
- **Deleting a stage or a variant can't leave a campaign broken.** Only
  the *last* stage in a sequence can ever be deleted (stages must stay
  contiguous from Intro, or a middle deletion would silently orphan
  every stage after it). A variant can only be deleted if more than one
  remains, and is always removed from every stage at once — never
  partially, which would break the "every stage offers the same
  variants" rule the rest of the system depends on.
- **A verified reply match is required to stop a sequence.** A message
  whose headers provably reference this system's own sent Message-ID
  can stop a sequence; a message matched only by sender email address is
  logged for visibility but can never stop anything on its own — someone
  sharing an inbox, or a spoofed sender, should never silently end a
  real campaign.
- **A low-confidence AI classification is never trusted at face value.**
  If the sales-intent classifier isn't confident, the result always
  shows as "Unclear," never a specific guess that could lead to treating
  a real prospect as a lost cause.

## 10. Reliability features

- **Every Google Sheets call automatically retries** on a transient
  rate-limit or server error, with exponential backoff — a random
  Google-side hiccup no longer fails an entire scheduled run.
- **Message-ID deduplication** means a reply is only ever logged, and
  only ever intent-classified, exactly once — re-running `check_replies`
  a hundred times never processes the same message twice.
- **One account's IMAP outage never blocks the others** — `check_replies`
  isolates each account, logging the failure and moving on rather than
  letting one broken connection stop every account's replies from being
  checked that run.
- **A campaign's header row can safely gain new columns** as the system
  evolves without breaking any Sheet created before that column existed.
- **A bulk CSV account upload never lets one bad row block the rest** —
  a typo in row 47 of a 500-row file is reported and skipped; the other
  499 accounts still get added.

---

## 11. Testing

**873 automated tests** (321 for the core engine, 552 for the Streamlit
app), run on every push via `ci.yml`, with a deliberate testing
methodology worth calling out: for anything genuinely safety-critical —
the Bcc-invisibility guarantee, the typed-confirmation gates, the
low-confidence-downgrades-to-Unclear rule, the "only the last stage can
be deleted" constraint — the relevant protection was **deliberately
broken on purpose**, confirmed that a test caught it, and only then
restored. A passing test suite alone doesn't prove a safeguard actually
works; proving the failure mode is caught does.

Every Streamlit page is tested by actually running it (via Streamlit's
own `AppTest` framework) against realistic fake data, not just testing
its underlying logic in isolation — catching real integration bugs
(import errors, wrong widget wiring, a value silently not reaching the
function it needed to) that unit tests of the logic alone can't see.

## 12. Technology stack

| Layer | Technology |
|---|---|
| Core engine | Python 3.11, plain `argparse` CLI, no heavy framework |
| Data store | Google Sheets, via `gspread` + a Google Cloud service account |
| Execution | GitHub Actions (`workflow_dispatch` + `schedule` triggers) |
| Email sending | `smtplib` (SMTP over SSL), custom host/port/username per account |
| Reply detection | `imaplib` (IMAP over SSL), same per-account customization |
| Sales intent classification | Anthropic API (Claude Haiku), optional |
| Control panel | Streamlit (multi-page app, `st.cache_data`/`st.cache_resource`) |
| Repo/credential operations | GitHub REST API via `requests`, with `PyNaCl` for the libsodium sealed-box encryption GitHub's Secrets API requires |
| Testing | `pytest`, `streamlit.testing.v1.AppTest` |

## 13. What this gets you, concretely

- **No SaaS subscription.** GitHub Actions' free tier and Streamlit
  Community Cloud comfortably cover typical usage; the only recurring
  cost is whatever you spend on the optional AI classification calls,
  entirely under your control.
- **You own the data, completely.** Every lead, every sent email, every
  reply lives in a Google Sheet and a GitHub repo you control — nothing
  is locked inside a vendor's platform you'd need to export from if you
  ever left.
- **A full audit trail, readable by anyone.** Every send, every reply,
  every error is a row in a Sheet a non-technical person can open
  directly — no separate reporting layer needed.
- **Real multi-provider support.** Gmail and any SMTP/IMAP provider
  (Hostinger and others) work side by side, each account with its own
  host, port, and credentials where needed.
- **Nothing sends by accident.** Approval gates, typed confirmations,
  status checks, and header-verified reply matching all exist
  specifically so a slip of the mouse can't send 500 emails or end a
  real campaign on a false match.
- **It scales to how you actually work.** Adding the 501st email account
  is a CSV upload, not 500 individual form submissions; managing
  hundreds of daily replies is a filterable, searchable inbox, not a
  raw spreadsheet.

## 14. Known limitations (by design, not oversights)

- **No true real-time reply detection.** Checking is on a schedule
  (every 10 minutes) or on demand, not push-based — deliberately, since
  IMAP push isn't universally available across providers and would mean
  maintaining a different mechanism per provider for a fairly small gain
  over the current approach.
- **Sales intent classification needs an Anthropic API key to do
  anything** — without one, the Intent columns simply stay blank, and
  every reply still gets its full mechanical classification regardless.
- **A campaign's "read/unread" state syncs in batches**, not
  instantly — an explicit "Sync read status" step, so opening a dozen
  replies doesn't trigger a dozen separate GitHub Actions runs.
- **Custom email providers store the SMTP/IMAP password in a single
  GitHub Secret per account** — this is deliberate (one secret can be
  fully replaced without needing to know or reconstruct any other
  account's credentials), but it does mean a provider with genuinely
  different SMTP vs. IMAP passwords needs both entered explicitly.

## 15. One-time setup checklist

1. **Google Cloud service accounts** — one with Editor access (used by
   GitHub Actions to write to the Sheet) and one with Viewer-only access
   (used by Streamlit to read it).
2. **A GitHub fine-grained personal access token**, scoped to this
   repository only, with `Actions: read/write` and `Contents: read/write`
   — add `Secrets: write` only if you want in-app email account
   management.
3. **Repository secrets**: `GOOGLE_SERVICE_ACCOUNT_JSON`, at least one
   email account (`EMAIL_ACCOUNTS_JSON` or the numbered
   `EMAIL_ACCOUNT_SLOT_N` secrets), and optionally `ANTHROPIC_API_KEY`.
4. **Streamlit Secrets** — the GitHub token, the Viewer-only Google
   credential, and your login username/password for the app itself.
5. Deploy the Streamlit app pointed at this repository, and you're
   running.

---

*This document describes the system as it exists today — every feature,
workflow, and safeguard listed here is built, tested, and already part
of the live application, not a roadmap item.*
