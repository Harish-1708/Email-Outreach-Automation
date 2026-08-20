# Outreach Automation

Send cold outreach sequences (intro + follow-ups) from a Google Sheet,
controlled entirely through GitHub Actions. You decide who gets emailed,
what stage, and how many — the system handles personalization, spacing,
duplicate protection, and reply/bounce detection.

**Runs 100% on GitHub Actions. Nothing runs on your computer, ever** — not
even a one-time setup script. Setup is: create a Google Sheet, create a
Google service account, generate an App Password, paste values into GitHub
Secrets, edit one YAML file, push.

---

## 1. How it works, in one picture

```
Google Sheet (one sheet, auto-created tabs per campaign)
        |
        |  you trigger from GitHub Actions
        v
Preview Batch  -->  you review  -->  Send Batch (type "SEND")
        |
        v
   Email sent (SMTP)          Check Replies (every 30 min, IMAP)
        |                              |
        v                              v
  Sheet updated  <--------------  reply classified,
  (sent, variant,                 lead stopped if it's
  next eligible date)             a genuine reply/bounce
```

You never let it run unattended for a whole list. Every batch is: you pick
a campaign + stage + how many leads, GitHub shows you exactly what would
be sent, and only then do you tell it to actually send.

---

## 2. One-time setup

### Step 1 — Create one Google Sheet

Create a single blank Google Sheet. Copy its ID from the URL:
`https://docs.google.com/spreadsheets/d/`**`THIS-PART`**`/edit`

This one sheet holds every campaign you ever run, in separate tabs.

### Step 2 — Give the system access to the Sheet

1. In [Google Cloud Console](https://console.cloud.google.com/), enable the **Google Sheets API**.
2. Create a **Service Account**, then create a **JSON key** for it and download it.
3. Open your Sheet → **Share** → paste in the service account's email
   address (it's the `client_email` field inside the JSON you downloaded)
   → give it **Editor** access.
4. In your GitHub repo, go to **Settings → Secrets and variables →
   Actions**, and add a secret:
   - Name: `GOOGLE_SERVICE_ACCOUNT_JSON`
   - Value: the entire contents of that JSON file, pasted as-is.

Nothing further needed here — the actual tabs (Master/Responses/SendLog)
are created automatically the first time you run a campaign. There's no
separate "initialize the sheet" step.

### Step 3 — Create an App Password for each email account you'll send from

For **every** Gmail or Google Workspace address you want to send from:

1. Turn on **2-Step Verification** on that account (Google requires this
   for App Passwords).
2. Go to `https://myaccount.google.com/apppasswords`, create a new App
   Password (name it anything, e.g. "Outreach"), and copy the
   16-character code it gives you.

Then combine every account you set up into **one** GitHub secret:

- Name: `EMAIL_ACCOUNTS_JSON`
- Value (edit to match your real accounts):

```json
{
  "sales1": {"address": "sales1@yourdomain.com", "app_password": "abcd efgh ijkl mnop"},
  "sales2": {"address": "sales2@yourdomain.com", "app_password": "qrst uvwx yzab cdef"}
}
```

You can list one account or many — the whole point of this design is that
adding or removing an account is just editing this one secret. Nothing
else needs to change.

### Step 4 — Edit `config/campaigns.yaml`

Open `config/campaigns.yaml` and set two things at the top:

```yaml
shared_sheet_id: "PUT_YOUR_GOOGLE_SHEET_ID_HERE"   # from Step 1

email_accounts:
  default_account: "sales1"   # which account key from EMAIL_ACCOUNTS_JSON
                                # to use when a lead doesn't specify one
```

The repo ships with one working example campaign, `sample_campaign`, ready
to use as-is. To customize it (or add more campaigns), see [Section 5](#5-configuring-a-campaign).

### Step 5 — Edit your email templates

Templates live in `templates/sample_campaign/` — 20 files (5 stages ×
4 variants: A/B/C/D). Open each one and replace the bracketed placeholders
(`[Your Name]`, `[what you do]`, `[specific value point]`, etc.) with your
real content. See [Section 6](#6-templates) for the exact format.

### Step 6 — Push to GitHub

Push the repo with both secrets set (`GOOGLE_SERVICE_ACCOUNT_JSON`,
`EMAIL_ACCOUNTS_JSON`). That's the entire setup — you're ready to run.

---

## 3. Adding leads

Once you've run `Preview Batch` at least once (see Section 4), your Sheet
will have three new tabs: `sample_campaign_Master`,
`sample_campaign_Responses`, `sample_campaign_SendLog`. Add your leads as
rows in the **Master** tab, below the header row.

**Only the `Email` column is required.** Everything else is optional:

| Column | Required? | What to put in it |
|---|---|---|
| `LeadID` | Optional | Any identifier you like (e.g. `L001`) — purely for your own reference |
| `FirstName` | Optional | Blank renders as "there" in templates ("Hi there,") |
| `LastName` | Optional | Blank is just left out |
| `Email` | **Required** | The only mandatory field. No email = never eligible for anything |
| `Company` | Optional | Blank renders as "your team" |
| `Campaign` | Optional | Not read by the system — just a label for your own reference |
| `Approval` | Leave blank or set to `Yes` | See table below |
| `SenderAccount` | Optional | Which account (a key from `EMAIL_ACCOUNTS_JSON`) sends to this lead. Blank = use the default. Gets filled in automatically after the first send, so later stages keep using the same account |
| `RequestedAction` | Optional | Free text, **not read by the system** — a place for your own notes |

Every other column (`CurrentStage`, `IntroSentAt`, `Status`, etc.) is
**written by the system, not by you** — leave those blank when adding a
new lead.

### The `Approval` column

| Value | Meaning |
|---|---|
| *(blank)* or `Pending` | Ignored completely — never picked up by any batch |
| `Yes` | Eligible to be picked up in a batch (still requires you to run `Send Batch` — approval alone never sends anything) |
| `No` | Permanently excluded |
| `Paused` | Temporarily excluded, without deleting the row |

So: to actually email someone, you must add them with `Approval` set to
`Yes`. Everything else about a lead can be left blank.

---

## 4. Running it, day to day

Everything happens from your repo's **Actions** tab.

### Preview Batch

Run this first, always. Inputs: campaign name, stage (`intro`,
`followup1`, `followup2`, `followup3`, `followup4`), and how many leads.
It shows you exactly who's eligible and exactly what each email will say
— **nothing is sent**. Check the job summary on the run page.

### Send Batch

Same inputs, plus you must type `SEND` (exact match) in the confirm
field, or the workflow refuses to run. This is the only thing that
actually sends email. It sends one by one with a random delay between
each (configurable — see Section 5), and updates the Sheet as it goes.

### Check Replies

Runs automatically every 30 minutes — checks every account in
`EMAIL_ACCOUNTS_JSON` for new mail, logs everything to that campaign's
`Responses` tab, and stops a lead's sequence if the message is a genuine
reply or a hard bounce. You can also trigger it manually from Actions.

### A normal week looks like:

1. Add 50 new leads to Master, `Approval = Yes`.
2. Run **Preview Batch**: campaign `sample_campaign`, stage `intro`,
   batch size `20`. Read the job summary.
3. Happy with it → run **Send Batch** with the same inputs, type `SEND`.
4. `Check Replies` runs on its own every 30 min from here.
5. A few days later, check `NextEligibleAt` in Master to see who's ready
   for `followup1`, and repeat from step 2 with that stage.

---

## 5. Configuring a campaign

Everything lives in `config/campaigns.yaml`:

```yaml
shared_sheet_id: "..."          # your one Google Sheet, shared by all campaigns

email_accounts:
  default_account: "sales1"     # fallback sending account

campaigns:
  sample_campaign:                          # <- this is the "campaign name" you
                                             #    type into the workflow inputs
    templates_dir: "templates/sample_campaign"
    variants: ["A", "B", "C", "D"]

    stages:                                  # 1 to 5 stages, any names/order
      - name: intro
        template_prefix: intro
        wait_days_after_previous: 0
      - name: followup1
        template_prefix: followup1
        wait_days_after_previous: 3          # days after Intro before eligible
      # ...

    sending:
      timezone: "Asia/Kolkata"
      window_start: "09:00"                  # advisory — not a hard block
      window_end: "17:00"
      delay_min_minutes: 3                    # random delay between sends
      delay_max_minutes: 7
      daily_limit: 100                        # hard cap per campaign per day

    reply_monitor:
      lookback_hours: 24                      # how far back Check Replies looks
```

### Adding a second campaign

Just add another block under `campaigns:`. Its three tabs
(`<name>_Master`, `<name>_Responses`, `<name>_SendLog`) get created
automatically in the same shared sheet the first time you run
`Preview Batch` for it. Nothing else to set up.

### Optional per-campaign overrides

Uncomment any of these inside a campaign block if you need them:

```yaml
    sheet_id: "A_DIFFERENT_SHEET_ID"     # use a different sheet just for this campaign
    master_tab: "CustomName"             # override the auto-generated tab name
    responses_tab: "CustomName"
    send_log_tab: "CustomName"
    default_sender_account: "sales2"     # override the global default account
```

---

## 6. Templates

Each stage has 4 variant files in `templates/<campaign>/`, named
`<stage>_<variant>.txt` — e.g. `intro_A.txt`, `followup1_C.txt`. The
system picks whichever variant has been used least so far for that stage,
so your 4 versions stay evenly distributed across your leads.

**File format** — first line is the subject, then a blank line, then the body:

```
Subject: Quick idea for {{CompanyName}}

Hi {{FirstName}},

I came across {{CompanyName}} recently and wanted to reach out...

Best,
[Your Name]
```

**Available variables:**

| Variable | Comes from column | If blank |
|---|---|---|
| `{{FirstName}}` | `FirstName` | "there" |
| `{{LastName}}` | `LastName` | (empty) |
| `{{CompanyName}}` | `Company` | "your team" |
| `{{Email}}` | `Email` | always present (mandatory) |

Bracketed text like `[Your Name]` or `[what you do]` is **not** a
variable — it's just a placeholder for you to manually replace with your
own content before using the templates for real.

Follow-up templates automatically reply within the same email thread as
the Intro (proper `In-Reply-To`/`References` headers), so the recipient
sees one coherent conversation, not disconnected emails.

To force one specific variant instead of the auto-balanced rotation (e.g.
for testing), use the `variant` input on the Preview/Send workflows
(default `Auto`).

---

## 7. The three sheet tabs, in full

### `<campaign>_Master` — one row per lead, current state

| Column | Set by | Meaning |
|---|---|---|
| `LeadID` | You | Your own reference ID |
| `FirstName`, `LastName`, `Company` | You | Optional — see Section 3 |
| `Email` | You | **Required** |
| `Campaign` | You | Label only, not read by the system |
| `Approval` | You | `Pending`/`Yes`/`No`/`Paused` — see Section 3 |
| `SenderAccount` | You (optional) / System | Which account sends to this lead; locked in after first send |
| `RequestedAction` | You | Free text, not read by the system |
| `CurrentStage` | System | Most recent stage sent |
| `ScheduledAt` | — | Reserved, not currently used |
| `IntroSentAt` ... `FollowUp4SentAt` | System | Timestamp each stage was sent (blank = not sent yet) |
| `IntroVariant` ... `FollowUp4Variant` | System | Which variant (A/B/C/D) each stage used |
| `NextEligibleAt` | System | When this lead becomes eligible for the *next* stage |
| `ReplyStatus` | System | Blank or `Replied` |
| `ReplyAt` | System | When a genuine reply was detected |
| `LastInboundClassification` | System | Most recent inbound message's classification |
| `LastInboundAt` | System | Timestamp of that |
| `Status` | System | Human-readable state: `Intro Sent`, `Stopped - Replied`, `Stopped - Bounced`, `Paused`, etc. |
| `LastActionAt` | System | Timestamp of the most recent send/reply/bounce event |
| `Error` | System | Last error for this lead, if any (send failure, unknown account, etc.) |
| `MessageID` | System | Message-ID of the most recent email sent to this lead |
| `ThreadReferences` | System | Accumulated email-thread reference chain |

### `<campaign>_Responses` — one row per inbound message ever detected

| Column | Meaning |
|---|---|
| `ResponseID` | Unique ID (the message's Message-ID) |
| `LeadID` | Which lead this matched to |
| `Campaign` | Campaign name |
| `ReceivedAt` | When it was processed |
| `From` | Sender address |
| `Subject`, `Snippet` | From the message |
| `Classification` | `Genuine Reply` / `Auto-Reply` / `Out of Office` / `Bounce (Hard)` / `Bounce (Soft)` |
| `MatchMethod` | `Header` (matched via In-Reply-To/References — strong) or `Email` (matched by sender address — fallback) |
| `MessageID`, `InReplyTo` | Raw email headers, for your own auditing |
| `ActionTaken` | `Stopped Sequence` or `Logged Only` |

Every inbound message is logged here regardless of classification. Only
`Genuine Reply` and `Bounce (Hard)` actually stop a lead's sequence —
auto-replies, out-of-office messages, and soft bounces are recorded but
don't interrupt anything.

### `<campaign>_SendLog` — one row per outbound send attempt, ever

| Column | Meaning |
|---|---|
| `BatchID` | Groups every email from one `Send Batch` run (e.g. `BATCH-20260819-103000`) |
| `Timestamp`, `LeadID`, `Email`, `Campaign`, `Stage`, `Variant`, `SenderAccount` | What was sent, to whom, from which account |
| `Status` | `sent` or `error` |
| `MessageID` | If sent successfully |
| `Error` | If it failed |

This answers "what did I send yesterday" or "which leads were in that
batch" without reconstructing it from Master.

---

## 8. Safety behavior worth knowing

- **Duplicate protection** — a lead that already has a timestamp in a
  stage's `SentAt` column can never be sent that stage again, even if you
  re-run the same batch by accident.
- **Reply/bounce = automatic stop** — enforced at the eligibility check
  itself, not just hidden in a UI. A replied or hard-bounced lead is
  filtered out of every future batch.
- **Per-lead error isolation** — one lead failing (bad address, unknown
  sender account, SMTP hiccup) never aborts the rest of the batch, and
  that lead's `SentAt` is never written on failure, so it stays eligible
  for a retry.
- **Daily send limit** — `Send Batch` automatically caps itself if you're
  close to `sending.daily_limit` for that campaign.
- **Confirm-to-send gate** — `Send Batch` refuses to run unless you type
  `SEND` exactly.
- **One IMAP account failing doesn't block the others** — `Check Replies`
  checks every account independently; a problem with one account's inbox
  just logs a warning and moves on to the next.

---

## 9. Multi-account sending

Every lead's `SenderAccount` column can name any account key from
`EMAIL_ACCOUNTS_JSON`. Resolution order:

1. The lead's own `SenderAccount` cell, if set.
2. The campaign's `default_sender_account` (optional, in `campaigns.yaml`).
3. The global `email_accounts.default_account`.

Whichever account is used gets **written back into the lead's
`SenderAccount` cell** after the first successful send — so every later
stage automatically reuses that same account, keeping a consistent
sender identity across the whole conversation with that lead.

`Check Replies` checks **every** configured account's inbox on every run,
since a reply could land in any of them.

> Gmail/Workspace sending limits apply per account regardless of how many
> you rotate through — multiple accounts are for legitimate organizational
> reasons, not for evading anti-abuse limits.

---

## 10. Project structure

```
outreach.py                 Everything: CLI, Sheets, SMTP send, IMAP read,
                             templating, eligibility, classification
config/campaigns.yaml       shared_sheet_id + email_accounts + campaigns
templates/sample_campaign/  20 template files (5 stages x 4 variants)
tests/test_outreach.py      57 unit tests
.github/workflows/
  preview_batch.yml         Manual — shows what would be sent
  send_batch.yml            Manual, requires typing SEND — actually sends
  check_replies.yml         Automatic every 30 min, or manual
  ci.yml                    Runs the test suite on every push
requirements.txt
```

Sending (SMTP) and reading (IMAP) use Python's standard library — no
external email API dependency. Only Google Sheets access needs a package
(`gspread` + `google-auth`, for the service account).

---

## 11. Running the tests yourself (optional)

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

This is the only thing in this project you might ever run locally, and
it's entirely optional — it doesn't touch your real Sheet or send any
real email, it just verifies the code's logic against fakes.
