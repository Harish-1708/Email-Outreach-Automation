# Outreach Control Panel (Streamlit)

A control surface on top of `outreach.py` + GitHub Actions — not a second
sending system. Preview runs the exact same code in-app (read-only, no
SMTP credentials involved). Send and Check Replies trigger the real GitHub
Actions workflows, with the same typed-`SEND` confirmation gate. New
Campaign opens a pull request; nothing is ever committed straight to
`main`.

## What each page does

- **📊 Dashboard** — read-only. Uses a Viewer-scoped Google credential and
  the exact same `outreach.compute_campaign_dashboard` math the Sheet's own
  Dashboard tab uses, so the two always agree.
- **🚀 Controls** — Preview (instant, in-app, nothing sent/written), Send
  (triggers `send_batch.yml`, requires typing `SEND`), Check Replies
  (triggers `check_replies.yml`).
- **➕ New Campaign** — creates `templates/<name>/intro_*.txt` and opens a
  PR. A human still merges it before the campaign is live.

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
- `Actions`: Read and write (needed for Send/Check Replies/status polling)
- `Contents`: Read and write, `Pull requests`: Read and write (only if
  you're using the New Campaign page)

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
secrets.toml to the repo).

## Known limitations (by design, not bugs)

- **No persistent login session.** Username/password here is intentionally
  simple — no OAuth means no "forgot password" flow and no cross-session
  cookie. Closing the tab logs you out. If this becomes annoying,
  `streamlit-authenticator` (cookie-based) or Streamlit's native
  `st.login()`/OIDC are the upgrade paths.
- **Run status is manual-refresh, not live-streaming.** After triggering
  Send/Check Replies, click "Refresh run status" — this app doesn't
  auto-poll in the background. A link to the full GitHub Actions run is
  always shown for complete logs.
- **New Campaign only creates the Intro stage.** Auto-discovery treats a
  single-stage campaign as fully valid. Add follow-up stages later via a
  normal PR (by hand, or a future extension of this page).

## Testing

`streamlit_app/tests/` covers every non-UI module (auth, github_client,
sheets_readonly, preview_logic's pure pieces, send_logic, campaign_builder)
with mocked HTTP/Sheets calls — no real network, no real credentials
needed. Run with:

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

**Dashboard (read-only — safe to test freely)**
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

**New Campaign (opens a real PR — merge or close it after testing)**
- [ ] A duplicate campaign name is rejected before any API call is made.
- [ ] Submitting opens a real pull request with the new
      `templates/<name>/intro_*.txt` file(s) — visible in the repo's Pull
      Requests tab, NOT already merged.
- [ ] The campaign does **not** appear in the Dashboard/Controls campaign
      list until the PR is merged (confirms auto-discovery is reading
      `main`, not the PR branch).
- [ ] After merging, the campaign appears and Preview works against it.

If any step fails, check the specific module it exercises (`auth.py`,
`github_client.py`, `sheets_readonly.py`) against the automated tests for
that module first — a live-only failure usually means a secrets/permission
mismatch, not a logic bug (the logic is what the 56 automated tests cover).
