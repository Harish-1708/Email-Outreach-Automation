# Outreach Automation

Send cold outreach sequences (intro + follow-ups) from a Google Sheet,
controlled entirely through GitHub Actions. You decide who gets emailed,
what stage, and how many — the system handles personalization, spacing,
duplicate protection, reply/bounce detection, dashboards, and structured
error monitoring.

**Runs 100% on GitHub Actions. Nothing runs on your computer, ever.**
Setup is: create a Google Sheet, create a Google service account, generate
an App Password, paste values into GitHub Secrets, edit one small settings
file, push.

**Launching a brand-new campaign needs no code or config changes at all** —
create a folder of templates and type the name into a workflow input. See
Section 5.

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
  Sheet updated              reply matched + classified, lead
  (sent, variant,             stopped ONLY on a verified match
  next eligible date)         to THIS campaign's own thread
        |                              |
        +----------> Dashboard refreshed automatically
                      (after every Send / Check Replies,
                      plus every 6 hours as a backstop)
```

Every batch is: you pick a campaign + stage + how many leads, GitHub shows
you exactly what would be sent, and only then do you tell it to actually
send.

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

Nothing further needed — each campaign's 5 tabs (see Section 7) are
created automatically the first time it runs.

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

List one account or many — adding/removing an account is just editing
this one secret.

### Step 4 — Edit `config/settings.yaml`

```yaml
shared_sheet_id: "PUT_YOUR_GOOGLE_SHEET_ID_HERE"   # from Step 1

email_accounts:
  default_account: "sales1"   # which EMAIL_ACCOUNTS_JSON key to use
                                # when a lead doesn't specify one
```

This is the **only** file every campaign shares. It also holds
`default_campaign_settings` — the stages, variants, and sending limits
every campaign inherits unless it overrides them. You won't normally need
to touch that part.

### Step 5 — Push to GitHub

Push with both secrets set. That's the entire one-time setup — see
Section 5 for how to actually launch a campaign.

---

## 3. Adding leads

Add leads as rows in a campaign's **Master Sheet** tab (created
automatically the first time you run `Preview Batch` for that campaign —
see Section 7 for the exact tab name).

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

Add extra columns to the Master Sheet **after** the required ones — e.g.
`Industry`, `JobTitle` — and reference them directly in templates as
`{{Industry}}`, `{{JobTitle}}`. No code changes needed. A blank value
renders as nothing, never as literal `{{Industry}}` text.

---

## 4. Running it, day to day

Everything happens from your repo's **Actions** tab.

### Preview Batch

Run this first, always. Shows exactly who's eligible and exactly what
each email will say — **nothing is sent**. Flags, per lead: any email
address that doesn't look correctly formatted, and any template variable
that didn't match a real column (a likely typo).

### Send Batch

Same inputs as Preview, plus you must type `SEND` (exact match). Sends in
**concurrent rounds**, not one email at a time — see Section 11 for
exactly how rounds are built. `daily_limit`, `per_account_daily_limit`,
and `sender_rotation` can all be overridden here **for this run only**
(see Section 10) — nothing you type here is ever written back to config.
Automatically refreshes that campaign's dashboard afterward.

### Check Replies

Runs automatically every 30 minutes — checks every account in
`EMAIL_ACCOUNTS_JSON`, logs everything to Responses, stops a lead's
sequence **only on a verified match to that campaign's own thread** (see
Section 9). Also refreshes the dashboard afterward.

### Update Dashboard

Manual (pick one campaign, or "update all"), plus runs automatically
every 6 hours as a backstop.

### A normal week looks like:

1. Add 50 new leads to the Master Sheet, `Approval = Yes`.
2. Run **Preview Batch**: stage `intro`, batch size `20`. Fix anything it
   flags.
3. Happy with it → run **Send Batch**, same inputs, type `SEND`.
4. `Check Replies` runs on its own from here.
5. Check the campaign's **Dashboard** tab anytime for current stats.
6. A few days later, check `NextEligibleAt` in Master Sheet to see who's
   ready for `followup1`, and repeat from step 2.

---

## 5. Launching a campaign

**A campaign exists the moment its templates folder exists — nothing
needs to be registered anywhere.** This is deliberate: the old design
required editing a shared YAML file to "register" every campaign before
it could run, which meant a real risk of launching in production having
forgotten that step. Now, the folder you'd have to create anyway (you
need template content regardless) *is* the registration.

**Stages and variants are auto-discovered from whichever template files
you actually create — 1 stage with 1 variant is a completely valid,
minimal campaign.** You are never required to build all 5 stages × 4
variants; that's just the maximum, not a minimum.

### Launching a brand-new campaign

1. Create `templates/<your_campaign_name>/` with your `.txt` template
   files — as few as one (`intro_A.txt`), or as many as 20 (5 stages × 4
   variants). See Section 6 for the exact format and how the shape is
   detected.
2. Type `<your_campaign_name>` into any workflow's `campaign` input.

That's it. No YAML edit required, regardless of how many stages or
variants you built.

If you type a campaign name with no matching templates folder at all, you
get a clear error immediately, before anything is attempted:

```
No templates found for campaign 'Foo' — expected a folder at
'templates/Foo'. Create it with your template files before running
this campaign. Currently available campaigns: Kelson_Creators_Licensing
```

### How the shape is detected

The system scans your templates folder in a fixed order — Intro, then
FollowUp1 through FollowUp4 — and stops at the first stage with no
template files at all. So:

| Files present | Discovered shape |
|---|---|
| `intro_A.txt` | 1 stage, 1 variant |
| `intro_A.txt`, `intro_B.txt` | 1 stage, 2 variants |
| `intro_A.txt`, `followup1_A.txt` | 2 stages, 1 variant each |
| All 20 (5 stages × A–D) | 5 stages, 4 variants |

Two rules keep this safe rather than silently permissive:

- **Stages must be contiguous from Intro.** If you have `intro` and
  `followup2` files but no `followup1` files, discovery stops at `intro`
  — it will never skip a gap.
- **Every discovered stage must offer the exact same variant letters as
  Intro.** A campaign with fewer variants *overall* is fine (e.g. just
  A). But if Intro has A/B/C/D and a later stage is missing just one of
  those — say, someone forgot `followup3_D.txt` — that's treated as a
  likely mistake, not an intentional design, and rejected with the exact
  missing filename. This is what actually prevents the risk of silently
  running a campaign with fewer variants than you think it has.

Wait times between auto-discovered stages come from
`config/settings.yaml`'s `default_campaign_settings.stage_wait_days`
lookup table (0 days for Intro, 3/4/5/5 for the follow-ups by default) —
edit that file if you want different default spacing across the board.

### Giving one campaign different settings

Most campaigns need nothing beyond the shared defaults. If one needs a
different daily limit, wait time, or sender, create
`config/campaigns/<campaign_name>.yaml` with **only what's different**:

```yaml
# config/campaigns/DudeRobe_Creator_Outreach.yaml
sending:
  daily_limit: 50
default_sender_account: "sales2"
```

Its stages/variants are still auto-discovered from the template files
exactly as if this file didn't exist — an override file doesn't opt you
out of auto-discovery by itself.

If you genuinely want to *force* an exact stage/variant shape instead
(strict validation, every implied file required to exist), declare
**both** `stages` and `variants` explicitly together in the override —
specifying only one raises a clear error. See
`config/campaigns/README.md` for that syntax and more examples.

### Seeing every campaign that currently exists

`dashboard --all` (manual or the 6-hourly scheduled run) discovers every
campaign by scanning `templates/` for subfolders — there's no separate
list to keep in sync. Whatever's in that folder is exactly what exists.

---

## 6. Templates

Templates live in `templates/<campaign>/`, named `<stage>_<variant>.txt`
— e.g. `intro_A.txt`, `followup1_B.txt`. How many stages and variants you
create is entirely up to that campaign (see Section 5) — the system picks
whichever variant has been used least so far for a given stage, keeping
however many versions you built evenly distributed.

**File format** — subject line, blank line, then body:

```
Subject: Quick idea for {{CompanyName}}

Hi {{FirstName}},

I came across {{CompanyName}} recently and wanted to reach out...

Best,
[Your Name]
```

**Leaving Subject blank continues the same thread instead of starting a new one** — for any stage after the first:

```
Subject: 

Just following up on my last note, {{FirstName}} — did you get a chance to look?
```

When a stage's rendered Subject is blank, the outgoing email uses `Re: <ThreadSubject>` instead — where `ThreadSubject` is whichever subject line this lead was *actually* sent most recently (tracked automatically in the Master Sheet's `ThreadSubject` column, no setup needed). Gmail/Outlook then group it with the earlier message in the same conversation, rather than starting a new one — the same convention tools like Instantly use.

A few things worth knowing:
- This is entirely your choice, per stage, per variant — leave Subject blank where you want to continue the thread, write a real Subject anywhere you want to deliberately start a fresh one (e.g. a follow-up 3 that intentionally reframes with a new angle). A non-blank Subject "resets" `ThreadSubject` going forward, so anything after *that* stage can continue from the new one with a blank Subject again.
- The very first stage (Intro) can never have a blank Subject — there's no previous thread to continue from a first message. A blank `intro_<variant>.txt` Subject fails loudly rather than guessing.
- If a later stage has a blank Subject but a specific lead somehow has no `ThreadSubject` recorded (this can only happen for leads sent to before this feature existed), that lead fails loudly with a clear fix: either put a Subject in the template, or fill in `ThreadSubject` manually for that one row in the Master Sheet.
- Preview and the CLI both show `(continuing existing thread)` next to any subject computed this way, so you can always see exactly what will go out before sending.

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
   Blank data for a real column just renders as nothing — normal, not
   flagged as an error.

3. **A variable matching no column at all** — almost always a typo.
   Renders as nothing, **and gets flagged**: shown as a warning in
   `Preview Batch`, and logged to the Error Log as `Missing Template
   Variable` after a real send.

Bracketed text like `[Your Name]` is **not** a variable — just a
placeholder for you to manually edit.

Follow-ups automatically reply within the same email thread as the Intro
(real `In-Reply-To`/`References` headers), so the recipient sees one
conversation, not disconnected emails. This threading is also what makes
reply matching trustworthy — see Section 9.

To force one specific variant instead of auto-rotation, use the `variant`
input on Preview/Send (default `Auto`).

---

## 7. The five sheet tabs, per campaign

Every campaign gets these, auto-created and auto-named from the campaign
folder name (e.g. for `Kelson_Creators_Licensing`):

| Tab | Default name | Purpose |
|---|---|---|
| Master | `Kelson_Creators_Licensing Master Sheet` | One row per lead — current state |
| Responses | `Kelson_Creators_Licensing Response Sheet` | One row per inbound message ever detected |
| Send Log | `Kelson_Creators_Licensing Custom Log Sheet` | One row per outbound send attempt ever made |
| Error Log | `Kelson_Creators_Licensing Error Log` | One row per error, categorized (see Section 10) |
| Dashboard | `Kelson_Creators_Licensing Dashboard` | Computed stats snapshot, rewritten on every refresh |

Plus one **shared, non-per-campaign** tab: **`All Campaigns Dashboard`** —
side-by-side comparison across every campaign, written by `dashboard --all`.

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
| `MessageID`, `ThreadReferences`, `ThreadSubject` | System | Email threading — see Section 6 (Subject continuation) and Section 9 |
| *(any extra columns you add)* | You | Available as custom template variables — see Section 6 |

### Response Sheet

| Column | Meaning |
|---|---|
| `ResponseID`, `LeadID`, `Campaign` | Identifiers |
| `ReceivedAt`, `From`, `Subject`, `Snippet` | From the inbound message |
| `Classification` | `Genuine Reply` / `Auto-Reply` / `Out of Office` / `Bounce (Hard)` / `Bounce (Soft)` |
| `MatchMethod` | `Header` (strong — see Section 9) or `Email` (weak, sender-only) |
| `MessageID`, `InReplyTo` | Raw headers, for your own auditing |
| `ActionTaken` | `Stopped Sequence` / `Logged Only` / `Logged Only (Unverified Match)` / `Logged Only (Predates Contact)` — see Section 9 |

### Custom Log Sheet (send history)

| Column | Meaning |
|---|---|
| `BatchID` | Groups every email from one `Send Batch` run |
| `Timestamp`, `LeadID`, `Email`, `Campaign`, `Stage`, `Variant`, `SenderAccount` | What was sent, to whom, from which account |
| `Status` | `sent`, `error`, or `skipped` (sender-capacity deferred — see Section 11) |
| `MessageID`, `Error` | Result details |

### Error Log

See Section 10.

### Dashboard

See Section 8. Fully rewritten on every refresh — a snapshot, not a
history log.

---

## 8. Dashboards

Each campaign's Dashboard tab is a 3-column `Section | Metric | Value`
sheet, recomputed fresh every time:

- **Overview** — Total Leads, Unique Leads Contacted, Total Emails Sent,
  Delivered (estimated), Bounced (Hard/Soft), Genuine Replies, Reply Rate,
  Sequence Completion Rate.
- **Per-Stage** — emails sent at each configured stage.
- **Sender Performance** — sent / replies / reply rate, broken down by
  `SenderAccount`.
- **Sender Usage Today** — whether `sender_rotation` is on, and each
  account's send count today (`used / cap` if `per_account_daily_limit`
  is set).
- **Variant Performance** — sent / replies / reply rate, broken down by
  stage + variant.
- **Errors (All Time)** — count of every error logged, by type.
- **Recent Errors** — the last 10 error log entries.

Two things worth knowing about the numbers:

- **"Delivered" is an estimate** (`Total Sent minus Hard Bounces`), not a
  real delivery receipt — SMTP doesn't provide confirmed-delivery data.
- **Variant reply attribution is approximate** — attributed to whichever
  stage/variant was that lead's most recent send at the time of a
  *verified* (Header-matched) reply.

Run `dashboard --all` to also write the shared **All Campaigns
Dashboard** — one row per campaign, discovered from `templates/`
subfolders (see Section 5), for comparison.

### About visual charts (roadmap, not built yet)

The Dashboard tab is tabular by design, rebuilt from scratch every
refresh. You can already select any range on it and insert a native
Google Sheets chart (Insert → Chart) today — it'll show current data as
long as the rows you selected keep meaning the same thing. Fully
auto-generating charts via the Sheets API is real, separate engineering
work (no high-level chart support in `gspread`, and charts need to
survive a tab that's fully cleared and rewritten every refresh) —
intentionally scoped as a future update, not attempted here.

---

## 9. Reply matching safety

This is the most important section if you're running more than one
campaign, or reusing lead lists across campaigns.

### The problem this section exists to prevent

Email replies from the same address can arrive that have nothing to do
with your current campaign — a lead's reply to an *old* campaign (or one
you've since deleted), sitting in the same shared inbox, from an address
that also happens to be a lead in your *new* campaign. Matching purely by
"this sender emailed us" cannot tell those apart, and incorrectly
stopping a live sequence — or reporting a false reply on your dashboard —
is a real production risk, not a theoretical one.

### How matching actually works

Every inbound message is checked against two signals, in order:

1. **Header match** — the message's `In-Reply-To` or `References`
   contains a `Message-ID` this system genuinely sent, for a lead in
   *this* campaign's own Master Sheet. This is provable: it means the
   message is literally part of a thread this campaign started. **Only a
   Header match can stop a sequence.**
2. **Email match** — the sender's address matches a lead, but there's no
   header link. This is real signal (logged, always visible), but is
   **never enough on its own** to stop a sequence, mark a reply, or
   affect the dashboard's reply count. It's a fallback for visibility,
   not a trigger.

For an Email-only match, one more check runs before deciding how to log
it: if the message's `Date` is **before** the most recent stage this
system actually sent to that lead, it's chronologically impossible for
it to be a reply to this campaign's outreach — logged as `Logged Only
(Predates Contact)`. Otherwise it's logged as `Logged Only (Unverified
Match)`. Either way, **`ReplyStatus` and `Status` are left untouched** —
the sequence keeps running.

| Match | Classification | What happens |
|---|---|---|
| Header | Genuine Reply | **Stops the sequence** |
| Header | Bounce (Hard) | **Stops the sequence** |
| Header | Auto-Reply / OOO / Bounce (Soft) | Logged only, sequence continues |
| Email | *(any)* | **Never stops anything** — logged as Unverified Match or Predates Contact |

### If a lead was already incorrectly stopped by this before the fix

This logic wasn't always this strict — earlier versions treated any
sender-email match the same as a header match. If a lead's `Status` shows
`Stopped - Replied` from before you had this fix, and you believe it was
a false positive (check the `Responses` tab's `MatchMethod` column for
that lead — `Email` with an unrelated `InReplyTo` is the signature),
you'll need to manually clear `ReplyStatus`, `Status`, and `ReplyAt` on
that row to let the sequence resume. This can't be done automatically —
there's no way to distinguish "this was a false positive" from "this was
correctly stopped" after the fact without a human looking at it.

---

## 10. Error monitoring

Every error anywhere in the system is classified and logged to that
campaign's **Error Log** tab:

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
| `Sender Capacity Reached` | Every account eligible for a lead is already at its `per_account_daily_limit` for today — see Section 11 |

**Two failure modes are flagged distinctly from ordinary errors, since
neither means something actually broke:**

- `sent_but_sheet_error` — the email genuinely sent, but the sheet update
  afterward failed, so that lead's `SentAt` never got recorded — risking
  a duplicate send next run. Logged with an explicit "check manually" note.
- `skipped` — the lead was eligible, but every account it could use was
  already at capacity for the day. Nothing was attempted or failed; it's
  retried automatically on a later run.

Ordinary errors never interrupt a batch — one lead's failure is isolated
and logged, and the batch continues to the rest.

---

## 11. Multi-account sending

Every lead's `SenderAccount` column can name any key from
`EMAIL_ACCOUNTS_JSON`. Resolution order:

1. **The lead's own `SenderAccount` cell, if set — always wins.** Even if
   `sender_rotation` is on. If that account is at its daily cap, the lead
   is **skipped**, never silently rerouted to a different account.
2. **Automatic rotation**, if `sending.sender_rotation: true` — picks
   whichever account has been used least today.
3. **The campaign's `default_sender_account`**, then the global
   `email_accounts.default_account`.

```yaml
sending:
  daily_limit: 100
  sender_rotation: true
  per_account_daily_limit: 5
  delay_min_minutes: 10
  delay_max_minutes: 10
  # rotation_accounts: ["sales1", "sales2"]   # omit to use every account
```

> All three of `sender_rotation`, `per_account_daily_limit`, and
> `daily_limit` can also be set directly as **`Send Batch` workflow
> inputs** for a single run, without editing any config — see Section 4.

### How sending is actually paced: concurrent rounds, not one-by-one

`Send Batch` sends in **rounds**, not one email at a time. A round is
built so that **no two leads in it share a sender account** — every lead
in a round is fired **at the exact same time**, over real simultaneous
SMTP connections, one send per account. Once a round finishes, the system
waits `delay_min_minutes`–`delay_max_minutes` (a random point in that
window), then builds and fires the next round.

This means `delay_min_minutes` / `delay_max_minutes` are the wait
**between rounds**, not between individual emails. In effect, each sender
account only sends its own next email one round-delay later — but
*different* accounts send simultaneously with each other.

Concretely, with `sender_rotation: true`, 50 configured accounts, a batch
of 500 leads, and `delay_min_minutes`/`delay_max_minutes` both set to
`10`:

- Round 1: 50 leads, one per account, sent **at the same time**.
- Wait ~10 minutes.
- Round 2: the next 50 leads, one per account, sent at the same time.
- ...continues until the batch (or `daily_limit`) is exhausted — 10
  rounds total for 500 leads / 50 accounts.

A lead whose only eligible account is already used by another lead **in
the current round** isn't an error — it's simply carried into the next
round automatically. Two leads manually pinned to the *same*
`SenderAccount` will always land in different rounds for this reason
(one account can't send two "simultaneous" emails).

**Single-account setups are unaffected.** With no rotation and no manual
pins spread across multiple accounts, every lead resolves to the same
one account, so every round ends up with exactly one lead in it —
identical in effect to sending one at a time with a delay between each,
exactly as before.

Whichever account is used gets **written back into the lead's
`SenderAccount` cell** after the first successful send, so every later
stage reuses it — a consistent sender identity across the whole
conversation with that lead.

`Check Replies` checks **every** configured account's inbox on every run.
One account's IMAP problem doesn't block the others.

> Gmail/Workspace sending limits apply per account regardless of how many
> you rotate through — multiple accounts are for legitimate organizational
> reasons, not for evading anti-abuse limits. Sending truly simultaneously
> from many accounts is standard practice for organizations running
> outreach at scale with dedicated, warmed-up sending accounts — but
> ramp up account count and daily volume gradually like you would with
> any cold-email program.

---

## 12. Safety behavior

- **Duplicate protection** — a lead with a timestamp already in a stage's
  `SentAt` column can never be sent that stage again.
- **Reply/bounce = automatic stop, only when verified** — see Section 9.
  Enforced at the eligibility check itself, not just hidden in a UI.
- **Per-lead error isolation** — one lead failing never aborts the batch;
  that lead's `SentAt` is never written on failure, so it's retry-eligible.
- **Daily send limit** — `Send Batch` automatically caps itself near
  `sending.daily_limit`.
- **Confirm-to-send gate** — `Send Batch` refuses to run without typing
  `SEND` exactly.
- **No template execution** — variables are plain string substitution,
  never evaluated as code.
- **Templates are validated, or auto-discovered — never guessed
  silently** — auto-discovery only includes a stage/variant if its file
  genuinely exists; an inconsistent shape (one stage missing a variant
  the others have) is rejected with the exact missing filename rather
  than quietly running a smaller campaign than intended. See Section 5.
- **Custom-column headers are relaxed, not silently accepted** — sheet
  tabs only require your header row to *start with* the system's
  required columns, in order. Extra trailing columns are fine; missing or
  reordered required columns raise a clear error.

---

## 13. Project structure

```
outreach.py                  Everything: CLI, Sheets, SMTP send, IMAP read,
                              templating, eligibility, reply-matching
                              safety, dashboards, error monitoring
config/
  settings.yaml               shared_sheet_id + email_accounts +
                               default_campaign_settings — the ONE shared
                               config file
  campaigns/                  optional per-campaign override files, named
                               <campaign_name>.yaml — most campaigns need
                               none at all
templates/
  Kelson_Creators_Licensing/  Template files — as few as 1 (intro_A.txt)
                               or up to 20 (5 stages x 4 variants). This
                               folder's EXISTENCE makes the campaign name
                               valid; its CONTENTS determine the shape
                               (auto-discovered — see Section 5)
tests/test_outreach.py        156 unit tests
streamlit_app/                Optional web control panel — see Section 15
.github/workflows/
  preview_batch.yml           Manual — shows what would be sent
  send_batch.yml               Manual, requires typing SEND — sends, then
                               refreshes that campaign's dashboard
  check_replies.yml            Automatic every 30 min, or manual
  dashboard.yml                 Manual (one campaign or --all), plus
                               automatic every 6 hours
  ci.yml                       Runs the test suite on every push
requirements.txt
```

SMTP/IMAP/email use Python's standard library — no external email API
dependency. Only Google Sheets access needs a package (`gspread` +
`google-auth`, for the service account).

---

## 14. Running the tests yourself (optional)

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

The only thing in this project you might ever run locally, and entirely
optional — it verifies the code's logic against fakes, never touches a
real Sheet or sends real email.

---

## 15. Optional web control panel (Streamlit)

`streamlit_app/` is an optional dashboard + control UI on top of everything
above — it is a **control surface, not a second sending system**:

- **Dashboard** — read-only view of every campaign (separate Viewer-scoped
  Google credential; can never write to the Sheet).
- **Controls** — Preview runs instantly in-app (reuses `outreach.py`'s own
  functions directly, no GitHub Actions round trip, no SMTP credentials
  anywhere near it). Send and Check Replies trigger the real
  `send_batch.yml` / `check_replies.yml` workflows — same safety checks,
  same typed-`SEND` confirmation, enforced in the UI itself.
- **New Campaign** — creates a campaign's Intro templates and opens a pull
  request. Nothing is ever committed directly to `main`; a human still
  merges it before the campaign goes live.

Login is username/password (hashed credentials in Streamlit Secrets) —
deliberately not Google/OAuth, so any colleague can log in with credentials
you hand them directly, at the cost of no persistent session (closing the
tab logs you out) and no "forgot password" flow.

This is entirely optional. Nothing about the core system (this README,
Sections 1–14) depends on it, and it can be deleted without affecting
`outreach.py` or any workflow in `.github/workflows/`.

Full setup instructions, the exact secrets needed, and a manual
verification checklist for a fresh deployment: see
[`streamlit_app/README.md`](streamlit_app/README.md).
