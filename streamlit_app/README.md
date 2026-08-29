# Outreach Control Panel (Streamlit)

A control surface on top of `outreach.py` + GitHub Actions — not a second
sending system. Preview runs the exact same code in-app (read-only, no
SMTP credentials involved). Send, Check Replies, and the Backfill tool
trigger the real GitHub Actions workflows, with the same typed-`SEND`
confirmation gate Send always had. New Campaign / Add Stage commits
directly — no GitHub trip, no pull request to approve.

## What each page does

- **🗂️ Campaigns** — the everyday view (Phases A, B, and C of the Campaigns
  Hub plan). Search, see every campaign's status at a glance, open one
  for Analytics (per-stage, per-variant, per-sender breakdowns) and Data
  (CSV upload with column mapping, a searchable/filterable lead table,
  and soft-remove — never a hard delete). Sequences, Schedule, Settings,
  and Responses are still honest placeholders — each says plainly which
  phase it's planned for.
- **📈 Overview** — every campaign at a glance: total leads, pending,
  sent, replies, reply rate.
- **📊 Dashboard** — read-only deep-dive into one campaign. Uses a
  Viewer-scoped Google credential and the exact same
  `outreach.compute_campaign_dashboard` math the Sheet's own Dashboard tab
  uses, so the two always agree.
- **🚀 Controls** — Preview (instant, in-app, nothing sent/written), Send
  (triggers `send_batch.yml`, requires typing `SEND`), Check Replies
  (triggers `check_replies.yml`, plus a live view of recent replies read
  straight from the Response Sheet), Maintenance (the ThreadSubject
  backfill tool).
- **📧 Email Accounts** — which sender accounts are configured and how
  much each has sent today, across all campaigns. Never the actual SMTP
  credentials — those stay in GitHub Secrets exclusively.
- **➕ New Campaign** — create a campaign's Intro templates, or add the
  next stage to an existing one. Commits straight to `main`; live the
  moment you click the button, no approval step.

## One-time setup

### 1. Deploy

Push this repo (must stay **private**) to GitHub, then on
[Streamlit Community Cloud](https://share.streamlit.io):
- New app → pick this repo → main file path: `streamlit_app/app.py`
- Deployed from a private repo, the app is private by default. Add
  colleagues as viewers under the app's "Share" menu if you also want
  GitHub/Google-identity gating in addition to the username/password login
  below (defense in depth, not required).

Free tier note: one private app, ~1GB memory, sleeps after 12h idle (next
visitor waits ~30s). Fine for occasional internal use; upgrade if that
becomes annoying.

### 2. Create a read-only Google service account

Separate from the one `GOOGLE_SERVICE_ACCOUNT_JSON` (GitHub Actions) uses.

1. In Google Cloud Console, create a second service account (e.g.
   `streamlit-readonly`).
2. Download its JSON key.
3. Open your Google Sheet → Share → paste that service account's
   `client_email` → give it **Viewer** access (not Editor).

### 3. Create a fine-grained GitHub token

Settings → Developer settings → Fine-grained personal access tokens → New
token, scoped to **only this repository**:
- `Actions`: Read and write (Send, Check Replies, Backfill, status
  polling)
- `Contents`: Read and write (only if you're using New Campaign / Add
  Stage — it commits template files directly)

No `Pull requests` permission needed — campaign creation no longer opens
one.

### 4. Set up login credentials

For each colleague:

```bash
python streamlit_app/tools/generate_password_hash.py
```

Paste the printed `[auth_users.<name>]` block into Streamlit Secrets. No
plaintext password is ever stored — only a salted PBKDF2 hash.

### 5. Fill in Streamlit Secrets

Copy `secrets.toml.example`, fill in real values, paste into the app's
Secrets settings in Streamlit Community Cloud (never commit a real
secrets.toml to the repo). Includes an optional `[email_accounts_directory]`
block (names + addresses only, no passwords) that powers the Email
Accounts page.

## Known limitations (by design, not bugs)

- **Leads are imported/removed via a commit-then-trigger-workflow
  pattern**, same as template creation — Streamlit commits a JSON payload
  file (`imports/<campaign>/...` or `removals/<campaign>/...`), then
  triggers `import_leads.yml` / `remove_leads.yml`, which does the actual
  Sheet write with the Editor-scoped credential and deletes the payload
  file afterward. Streamlit itself never gets Sheets write access, here
  or anywhere else in this app.
- **Removing a lead never deletes anything.** It sets `Status = Removed`
  — the row, and everything ever sent to that lead, stays intact. A
  removed lead is simply excluded from all future eligibility checks.
- **New leads always start as Pending approval**, even if your CSV had an
  "Approval" column you didn't map — approve them in the Data tab (or the
  Master Sheet directly) before they're eligible to send.

- **The Campaigns Hub's Data/Sequences/Schedule/Settings/Responses tabs
  are placeholders.** Only Analytics is real. Use Controls/New Campaign
  directly for anything those tabs would eventually cover — see
  `campaigns-hub-plan.md` for the phase each one is planned for.
- **A campaign's `status` (draft/active/paused) lives in its config
  override file** (`config/campaigns/<name>.yaml`), not the Sheet.
  Pausing/resuming currently means editing that file directly (a proper
  Pause/Resume button is Phase G). Unset `status` always means "active" —
  this was chosen specifically so introducing the field never silently
  paused a pre-existing campaign.
- **No persistent login session.** Username/password here is intentionally
  simple — no OAuth means no "forgot password" flow and no cross-session
  cookie. Closing the tab logs you out. If this becomes annoying,
  `streamlit-authenticator` (cookie-based) or Streamlit's native
  `st.login()`/OIDC are the upgrade paths.
- **Run status is manual-refresh, not live-streaming.** After triggering
  Send/Check Replies/Backfill, click "Refresh run status" — this app
  doesn't auto-poll in the background. A link to the full GitHub Actions
  run is always shown for complete logs.
- **New Campaign only creates the Intro stage** when starting a brand new
  campaign. Auto-discovery treats a single-stage campaign as fully valid.
  Use "Add the next stage to an existing campaign" (same page) to add
  follow-ups later.
- **Campaign creation is a direct commit, not a PR.** The in-app "I've
  reviewed this content" checkbox is the only remaining confirmation step
  — there's no second human review before it's live. If you want that
  back, the previous PR-based flow is straightforward to restore (open an
  issue/ask if you need it).

## Testing

`streamlit_app/tests/` covers every non-UI module (auth, github_client,
sheets_readonly, preview_logic's pure pieces, send_logic, campaign_builder,
overview_logic, replies_logic, accounts_logic) with mocked HTTP/Sheets
calls, plus page-level smoke tests that actually execute each page script
via Streamlit's own `AppTest` harness — no real network, no real
credentials needed. Run with:

```bash
cd streamlit_app
python -m pytest tests/ -v
```

This does **not** verify a live deployment — the Streamlit UI itself, the
real GitHub token, and the real Google credential all need one manual pass
against your actual repo/Sheet after deploying. Use the checklist below.

## Manual verification checklist (do this once, after deploying)

Everything above is verified with mocked Google/GitHub calls — this
section is the real pass against your actual repo, Sheet, and GitHub
Actions. Go through it in order; each step depends on the one before it.

**Setup**
- [ ] Read-only service account created, shared to the Sheet as **Viewer**
      (not Editor) — confirm in the Sheet's Share dialog.
- [ ] Fine-grained GitHub PAT created, scoped to **only this repo**.
- [ ] `secrets.toml` filled in on Streamlit Community Cloud and the app
      deploys without a "secrets not found" error.
- [ ] At least one user's hash generated via
      `tools/generate_password_hash.py` and added to `[auth_users]`.

**Login**
- [ ] Wrong password is rejected with an error, correct password logs in.
- [ ] 5 wrong attempts in a row lock you out (matches the automated test —
      confirming the real deployment behaves the same as the mocked one).
- [ ] "Log out" in the sidebar actually requires logging in again.
- [ ] Sidebar icons render correctly (📈 📊 🚀 📧 ➕), not as garbled text —
      if they still look broken, hard-refresh the browser tab first.

**Overview / Dashboard (read-only — safe to test freely)**
- [ ] Campaign selector lists your real campaign(s) from `templates/`.
- [ ] Numbers shown match the Sheet's own Dashboard tab (run
      `dashboard.yml` manually first if it hasn't run recently, so both
      are reading the same underlying data).
- [ ] "Refresh now" actually re-fetches (change something in the Sheet by
      hand, confirm it shows up after refresh, not just after 30s).

**Controls — Preview (safe, nothing is sent)**
- [ ] Preview returns the same eligible leads and rendered content you'd
      get from `python outreach.py preview` locally, for the same
      campaign/stage/batch size.
- [ ] A lead with `Approval` blank or `No` correctly does NOT appear.

**Controls — Send (uses a REAL test campaign / low batch size for this)**
- [ ] Submitting without typing `SEND` is rejected — no dispatch call
      happens (check the repo's Actions tab: no new run appears).
- [ ] Typing `SEND` and clicking Send actually triggers a real
      `send_batch.yml` run — confirm in the repo's Actions tab.
- [ ] "Refresh run status" reflects real progress (queued → in_progress →
      completed).
- [ ] The Sheet's Send Log gets the new row(s) after the run completes,
      and the Dashboard page reflects them after "Refresh now".

**Controls — Check Replies**
- [ ] Triggers a real `check_replies.yml` run, visible in the Actions tab.
- [ ] The "Recent replies" list shows real Response Sheet rows, with
      `ActionTaken` clearly labeled when a reply did NOT stop the sequence.

**Controls — Maintenance (Backfill)**
- [ ] Dry run shows what would be backfilled without writing anything.
- [ ] Turning off dry run and re-running actually writes `ThreadSubject`
      values to the Master Sheet.

**Email Accounts**
- [ ] Shows every account listed in `[email_accounts_directory]`, with
      today's real send count from the Send Log.

**New Campaign — creates a REAL campaign, live immediately**
- [ ] The "Create Campaign" / "Add Stage" button is disabled until the
      confirmation checkbox is checked.
- [ ] Submitting commits directly — check the repo's commit history, NOT
      the Pull Requests tab (there shouldn't be one).
- [ ] The campaign appears in Overview/Dashboard/Controls within a minute
      or two, with its Sheet tabs already created (no "tab doesn't exist"
      error) — this confirms the auto-triggered Dashboard-workflow tab
      initialization worked.
- [ ] "Add the next stage to an existing campaign" only offers variant
      letters matching the campaign's existing ones — try it on a
      multi-variant test campaign to confirm.

If any step fails, check the specific module it exercises (`auth.py`,
`github_client.py`, `sheets_readonly.py`, `campaign_builder.py`) against
the automated tests for that module first — a live-only failure usually
means a secrets/permission mismatch, not a logic bug (the logic is what
the automated test suite covers).
