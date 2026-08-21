# Outreach Automation

Send cold outreach sequences (intro + follow-ups) from a Google Sheet,
controlled entirely through GitHub Actions. You decide who gets emailed,
what stage, and how many — the system handles personalization, spacing,
duplicate protection, reply/bounce detection, dashboards, and structured
error monitoring.

**Runs 100% on GitHub Actions. Nothing runs on your computer, ever.**
Setup is: create a Google Sheet, create a Google service account, generate
an App Password, paste values into GitHub Secrets, edit one YAML file, push.

---

## 1. How it works, in one picture

```
Google Sheet (one sheet, 5 auto-created tabs per campaign)
        |
        |  you trigger from GitHub Actions
        v
Preview Batch  -->  you review  -->  Send Batch (type "SEND")
        |
        v
   Email sent (SMTP)          Check Replies (every 30 min, IMAP)
        |                              |
        v                              v
  Sheet updated              reply classified, lead stopped if
  (sent, variant,             it's a genuine reply/bounce
  next eligible date)                   |
        |                              v
        +----------> Dashboard refreshed automatically
                      (after every Send / Check Replies,
                      plus every 6 hours as a backstop)
```

Every batch is: you pick a campaign + stage + how many leads, GitHub shows
you exactly what would be sent, and only then do you tell it to actually send.

---

## 2. One-time setup

### Step 1 — Create one Google Sheet

Create a single blank Google Sheet. Copy its ID from the URL:
`https://docs.google.com/spreadsheets/d/`**`THIS-PART`**`/edit`

This one sheet holds every campaign you run, in separate tabs.

### Step 2 — Give the system access to the Sheet

1. In [Google Cloud Console](https://console.cloud.google.com/), enable the **Google Sheets API**.
2. Create a **Service Account**, then create a **JSON key** for it and download it.
3. Open your Sheet → **Share** → paste in the service account's email
   (the `client_email` field in the JSON) → give it **Editor** access.
4. In your GitHub repo, go to **Settings → Secrets and variables →
   Actions**, and add a secret:
   - Name: `GOOGLE_SERVICE_ACCOUNT_JSON`
   - Value: the entire contents of that JSON file, pasted as-is.

Nothing further needed — the 5 tabs per campaign (see Section 7) are
created automatically the first time you run that campaign.

### Step 3 — Create an App Password for each sending account

For **every** Gmail/Workspace address you want to send from:

1. Turn on **2-Step Verification** on that account.
2. Go to `https://myaccount.google.com/apppasswords`, create a new App
   Password, and copy the 16-character code.

Combine every account into **one** GitHub secret:

- Name: `EMAIL_ACCOUNTS_JSON`
- Value (edit to match your real accounts):

```json
{
  "sales1": {"address": "sales1@yourdomain.com", "app_password": "abcd efgh ijkl mnop"},
  "sales2": {"address": "sales2@yourdomain.com", "app_password": "qrst uvwx yzab cdef"}
}
```

List one account or many — adding/removing an account is just editing this
one secret.

### Step 4 — Edit `config/campaigns.yaml`

```yaml
shared_sheet_id: "PUT_YOUR_GOOGLE_SHEET_ID_HERE"   # from Step 1

email_accounts:
  default_account: "sales1"   # which EMAIL_ACCOUNTS_JSON key to use
                                # when a lead doesn't specify one
```

The repo ships with one working example, `sample_campaign`, ready to use
as-is — see [Section 5](#5-configuring-a-campaign) to customize it or add more.

### Step 5 — Edit your email templates

Templates live in `templates/sample_campaign/` — 20 files (5 stages ×
4 variants). Replace the bracketed placeholders (`[Your Name]`,
`[what you do]`, etc.) with your real content. See [Section 6](#6-templates).

### Step 6 — Push to GitHub

Push with both secrets set. That's the entire setup.

---

## 3. Adding leads

Add leads as rows in the **Master Sheet** tab (created automatically the
first time you run `Preview Batch` for a campaign — see Section 7 for the
exact tab name).

**Only the `Email` column is required.** Everything else is optional:

| Column | Required? | What to put in it |
|---|---|---|
| `LeadID` | Optional | Any identifier you like — your own reference |
| `FirstName` | Optional | Blank renders as "there" ("Hi there,") |
| `LastName` | Optional | Blank is just left out |
| `Email` | **Required** | No email = never eligible for anything |
| `Company` | Optional | Blank renders as "your team" |
| `Campaign` | Optional | Not read by the system — a label for your reference |
| `Approval` | Leave blank or set to `Yes` | See table below |
| `SenderAccount` | Optional | Which account sends to this lead. Blank = default. Auto-filled after the first send |
| `RequestedAction` | Optional | Free text, **not read by the system** — your own notes |

Every other column (`CurrentStage`, `IntroSentAt`, `Status`, etc.) is
**written by the system, not by you** — leave those blank on new rows.

### The `Approval` column

| Value | Meaning |
|---|---|
| *(blank)* or `Pending` | Ignored completely — never picked up by any batch |
| `Yes` | Eligible to be picked up in a batch (still requires you to run `Send Batch`) |
| `No` | Permanently excluded |
| `Paused` | Temporarily excluded, without deleting the row |

### Adding your own custom columns (e.g. Industry, Job Title)

You can add extra columns to the Master Sheet **after** the required ones
— e.g. `Industry`, `JobTitle`, `Region` — and reference them directly in
templates as `{{Industry}}`, `{{JobTitle}}`, etc. No code changes needed.
A blank value in one of these renders as nothing (not a literal
`{{Industry}}` in the email) — see Section 6 for exactly how this works.

---

## 4. Running it, day to day

Everything happens from your repo's **Actions** tab.

### Preview Batch

Run this first, always. Shows exactly who's eligible and exactly what
each email will say — **nothing is sent**. Also flags, per lead:
- Any email address that doesn't look correctly formatted.
- Any template variable that didn't match a real column (a likely typo) —
  these render as blank text in the actual email, so Preview is where you
  catch them before sending.

### Send Batch

Same inputs as Preview, plus you must type `SEND` (exact match). This is
the only thing that actually sends email — one by one with a random delay
between each, updating the Sheet as it goes. Automatically refreshes that
campaign's dashboard afterward.

### Check Replies

Runs automatically every 30 minutes — checks every account in
`EMAIL_ACCOUNTS_JSON`, logs everything to Responses, stops a lead's
sequence on a genuine reply or hard bounce. Also refreshes the dashboard
afterward. Can be triggered manually too.

### Update Dashboard

Manual (pick one campaign, or "update all"), plus runs automatically every
6 hours as a backstop, on top of the automatic refresh after every Send/
Check Replies run.

### A normal week looks like:

1. Add 50 new leads to the Master Sheet, `Approval = Yes`.
2. Run **Preview Batch**: stage `intro`, batch size `20`. Fix anything it
   flags (bad email formats, unrecognized variables).
3. Happy with it → run **Send Batch**, same inputs, type `SEND`.
4. `Check Replies` runs on its own from here.
5. Check the campaign's **Dashboard** tab anytime for current stats.
6. A few days later, check `NextEligibleAt` in Master Sheet to see who's
   ready for `followup1`, and repeat from step 2.

---

## 5. Configuring a campaign

Everything lives in `config/campaigns.yaml`:

```yaml
shared_sheet_id: "..."
email_accounts:
  default_account: "sales1"

campaigns:
  sample_campaign:                          # <- the "campaign name" you
                                             #    type into workflow inputs
    templates_dir: "templates/sample_campaign"
    variants: ["A", "B", "C", "D"]

    stages:                                  # 1 to 5 stages, any names/order
      - name: intro
        template_prefix: intro
        wait_days_after_previous: 0
      - name: followup1
        template_prefix: followup1
        wait_days_after_previous: 3
      # ...

    sending:
      timezone: "Asia/Kolkata"
      window_start: "09:00"                  # advisory — not a hard block
      window_end: "17:00"
      delay_min_minutes: 3
      delay_max_minutes: 7
      daily_limit: 100

    reply_monitor:
      lookback_hours: 24
```

### Adding a second campaign

Add another block under `campaigns:`. Its 5 tabs (see Section 7) get
created automatically in the same shared sheet the first time you run
`Preview Batch` for it.

### Optional per-campaign overrides

```yaml
    sheet_id: "A_DIFFERENT_SHEET_ID"
    master_tab: "CustomMasterName"
    responses_tab: "CustomResponsesName"
    send_log_tab: "CustomSendLogName"
    error_log_tab: "CustomErrorLogName"
    dashboard_tab: "CustomDashboardName"
    default_sender_account: "sales2"
```

---

## 6. Templates

Each stage has 4 variant files in `templates/<campaign>/`, named
`<stage>_<variant>.txt`. The system picks whichever variant has been used
least so far for that stage, keeping your 4 versions evenly distributed.

**File format** — subject line, blank line, then body:

```
Subject: Quick idea for {{CompanyName}}

Hi {{FirstName}},

I came across {{CompanyName}} recently and wanted to reach out...

Best,
[Your Name]
```

**How variables resolve** — in this order:

1. **Known variables** (`FirstName`, `LastName`, `CompanyName`, `Email`) —
   map to the matching Master Sheet column, with a friendly default if blank:

   | Variable | Column | If blank |
   |---|---|---|
   | `{{FirstName}}` | `FirstName` | "there" |
   | `{{LastName}}` | `LastName` | (empty) |
   | `{{CompanyName}}` | `Company` | "your team" |
   | `{{Email}}` | `Email` | always present (mandatory) |

2. **Any other `{{Variable}}`** — resolved directly against a Master Sheet
   column of the same name (your own custom columns, e.g. `{{Industry}}`).
   Blank data for a real column just renders as nothing — completely
   normal, not flagged as an error.

3. **A variable matching no column at all** — almost always a typo.
   Renders as nothing (never as literal `{{...}}` text in a real email),
   **and gets flagged**: shown as a warning in `Preview Batch`, and logged
   to the Error Log as `Missing Template Variable` after a real send.

Bracketed text like `[Your Name]` is **not** a variable — just a
placeholder for you to manually edit.

Follow-ups automatically reply within the same email thread as the Intro
(real `In-Reply-To`/`References` headers), so the recipient sees one
conversation, not disconnected emails.

To force one specific variant instead of auto-rotation, use the `variant`
input on Preview/Send (default `Auto`).

---

## 7. The five sheet tabs, per campaign

Every campaign gets these, auto-created and auto-named from the campaign
key (e.g. for `sample_campaign`):

| Tab | Default name | Purpose |
|---|---|---|
| Master | `sample_campaign Master Sheet` | One row per lead — current state |
| Responses | `sample_campaign Response Sheet` | One row per inbound message ever detected |
| Send Log | `sample_campaign Custom Log Sheet` | One row per outbound send attempt ever made |
| Error Log | `sample_campaign Error Log` | One row per error, categorized (see Section 9) |
| Dashboard | `sample_campaign Dashboard` | Computed stats snapshot, rewritten on every refresh |

Plus one **shared, non-per-campaign** tab: **`All Campaigns Dashboard`** —
side-by-side comparison across every configured campaign, written when you
run `dashboard --all` (the periodic scheduled run always does this).

### Master Sheet — full column reference

| Column | Set by | Meaning |
|---|---|---|
| `LeadID`, `FirstName`, `LastName`, `Company`, `Campaign` | You | Optional — see Section 3 |
| `Email` | You | **Required** |
| `Approval` | You | `Pending`/`Yes`/`No`/`Paused` |
| `SenderAccount` | You (optional) / System | Locked in after first send |
| `RequestedAction` | You | Free text, not read by the system |
| `CurrentStage` | System | Most recent stage sent |
| `ScheduledAt` | — | Reserved column, not currently written by any code path |
| `IntroSentAt` ... `FollowUp4SentAt` | System | Timestamp each stage was sent |
| `IntroVariant` ... `FollowUp4Variant` | System | Which variant (A/B/C/D) each stage used |
| `NextEligibleAt` | System | When eligible for the next stage |
| `ReplyStatus`, `ReplyAt` | System | Blank or `Replied`, and when |
| `LastInboundClassification`, `LastInboundAt` | System | Most recent inbound message's classification |
| `Status` | System | `Intro Sent`, `Stopped - Replied`, `Stopped - Bounced`, `Paused`, etc. |
| `LastActionAt` | System | Timestamp of the most recent send/reply/bounce |
| `Error` | System | Last error for this lead, if any |
| `MessageID`, `ThreadReferences` | System | Email threading headers |
| *(any extra columns you add)* | You | Available as custom template variables — see Section 6 |

### Response Sheet

| Column | Meaning |
|---|---|
| `ResponseID`, `LeadID`, `Campaign` | Identifiers |
| `ReceivedAt`, `From`, `Subject`, `Snippet` | From the inbound message |
| `Classification` | `Genuine Reply` / `Auto-Reply` / `Out of Office` / `Bounce (Hard)` / `Bounce (Soft)` |
| `MatchMethod` | `Header` (strong — matched via In-Reply-To/References) or `Email` (fallback) |
| `MessageID`, `InReplyTo` | Raw headers, for your own auditing |
| `ActionTaken` | `Stopped Sequence` or `Logged Only` |

Only `Genuine Reply` and `Bounce (Hard)` stop a lead's sequence — the rest
are logged but don't interrupt anything.

### Custom Log Sheet (send history)

| Column | Meaning |
|---|---|
| `BatchID` | Groups every email from one `Send Batch` run |
| `Timestamp`, `LeadID`, `Email`, `Campaign`, `Stage`, `Variant`, `SenderAccount` | What was sent, to whom, from which account |
| `Status` | `sent` or `error` |
| `MessageID`, `Error` | Result details |

### Error Log

See Section 9.

### Dashboard

See Section 8. This tab is **fully rewritten** on every refresh (not
appended to) — it's a snapshot of current state, not a history log.

---

## 8. Dashboards

Each campaign's Dashboard tab is a 3-column `Section | Metric | Value`
sheet, recomputed fresh every time. Sections:

- **Overview** — Total Leads, Unique Leads Contacted, Total Emails Sent,
  Delivered (estimated), Bounced (Hard/Soft), Genuine Replies, Reply Rate,
  Sequence Completion Rate.
- **Per-Stage** — emails sent at each configured stage.
- **Sender Performance** — sent / replies / reply rate, broken down by
  `SenderAccount`.
- **Variant Performance** — sent / replies / reply rate, broken down by
  stage + variant (e.g. `intro-A`).
- **Errors (All Time)** — count of every error logged, by type.
- **Recent Errors** — the last 10 error log entries.

Two things worth knowing about the numbers:

- **"Delivered" is an estimate** (`Total Sent minus Hard Bounces`), not a
  real delivery receipt — SMTP doesn't provide confirmed-delivery data.
  Labeled as an estimate rather than overclaiming precision.
- **Variant reply attribution is approximate.** A reply is attributed to
  whichever stage/variant was that lead's most recent send at the time
  they replied (their `CurrentStage`), since there's no per-message reply
  tracking at the individual-email level.

### The combined view across campaigns

Run `dashboard --all` (or wait for the 6-hourly scheduled run) to also
write the shared **All Campaigns Dashboard** tab — one row per campaign,
with the same core metrics side by side for comparison.

---

## 9. Error monitoring

Every error anywhere in the system is classified into one of these
categories and logged to that campaign's **Error Log** tab:

| ErrorType | When it fires |
|---|---|
| `Send Failure` | Generic SMTP send problem |
| `Authentication Failure` | Bad App Password / login rejected (SMTP or IMAP) |
| `Invalid Email Address` | Malformed address format, or the mail server rejected the recipient |
| `Sheets API Error` | A Google Sheets read/write call failed |
| `Rate-Limit Error` | SMTP responded with a rate-limit / "too many attempts" style error |
| `Missing Template Variable` | A `{{Variable}}` in a template matched no column (see Section 6) |
| `Missing Sender Account` | A lead's `SenderAccount` (or the configured default) doesn't exist in `EMAIL_ACCOUNTS_JSON` |
| `Reply Check Failure` | An IMAP problem during `Check Replies` that wasn't an authentication failure |

Every entry includes a timestamp, the campaign, the lead (if applicable),
the stage, the batch ID (if applicable), and a message with details.

**One failure mode is flagged more prominently than the rest:** if an
email sends successfully but the *sheet update afterward* fails (e.g. a
transient Sheets API error), that lead's `SentAt` never gets recorded —
which risks a duplicate send next run. This shows up as
`status: sent_but_sheet_error` in the `Send Batch` output and gets logged
with an explicit "check manually" note, so it's never silently confused
with either a normal successful send or a normal failed send.

Errors never interrupt a batch — one lead's failure is isolated and
logged, and the batch continues to the rest.

---

## 10. Multi-account sending

Every lead's `SenderAccount` column can name any key from
`EMAIL_ACCOUNTS_JSON`. Resolution order:

1. The lead's own `SenderAccount` cell, if set.
2. The campaign's `default_sender_account` (optional).
3. The global `email_accounts.default_account`.

Whichever account is used gets **written back into the lead's
`SenderAccount` cell** after the first successful send, so every later
stage reuses it — a consistent sender identity across the whole
conversation with that lead.

`Check Replies` checks **every** configured account's inbox on every run.
One account's IMAP problem doesn't block the others (logged to Error Log,
checking continues).

> Gmail/Workspace sending limits apply per account regardless of how many
> you rotate through — multiple accounts are for legitimate organizational
> reasons, not for evading anti-abuse limits.

---

## 11. Safety behavior

- **Duplicate protection** — a lead with a timestamp already in a stage's
  `SentAt` column can never be sent that stage again.
- **Reply/bounce = automatic stop** — enforced at the eligibility check
  itself, not just hidden in a UI.
- **Per-lead error isolation** — one lead failing never aborts the batch;
  that lead's `SentAt` is never written on failure, so it's retry-eligible.
- **Daily send limit** — `Send Batch` automatically caps itself near
  `sending.daily_limit`.
- **Confirm-to-send gate** — `Send Batch` refuses to run without typing
  `SEND` exactly.
- **No template execution** — variables are plain string substitution,
  never evaluated as code.
- **Custom-column headers are relaxed, not silently accepted** — Master/
  Responses/SendLog/Error Log tabs only require your header row to
  *start with* the system's required columns, in order. Extra trailing
  columns are fine (that's how custom variables work); missing or
  reordered required columns raise a clear error rather than silently
  writing into the wrong place.

---

## 12. Project structure

```
outreach.py                 Everything: CLI, Sheets, SMTP send, IMAP read,
                             templating, eligibility, classification,
                             dashboards, error monitoring
config/campaigns.yaml       shared_sheet_id + email_accounts + campaigns
templates/sample_campaign/  20 template files (5 stages x 4 variants)
tests/test_outreach.py      88 unit tests
.github/workflows/
  preview_batch.yml         Manual — shows what would be sent
  send_batch.yml            Manual, requires typing SEND — sends, then
                             refreshes that campaign's dashboard
  check_replies.yml         Automatic every 30 min, or manual — checks
                             replies, then refreshes the dashboard
  dashboard.yml             Manual (one campaign or --all), plus automatic
                             every 6 hours as a backstop
  ci.yml                    Runs the test suite on every push
requirements.txt
```

SMTP/IMAP/email use Python's standard library — no external email API
dependency. Only Google Sheets access needs a package (`gspread` +
`google-auth`, for the service account).

---

## 13. Running the tests yourself (optional)

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

The only thing in this project you might ever run locally, and entirely
optional — it verifies the code's logic against fakes, never touches a
real Sheet or sends real email.
