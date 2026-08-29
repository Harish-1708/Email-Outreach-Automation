import streamlit as st

from auth import login_gate, current_user, logout

st.set_page_config(page_title="Outreach Control Panel", page_icon="📬", layout="wide")

if not login_gate():
    st.stop()

with st.sidebar:
    st.success(f"Logged in as **{current_user()}**")
    if st.button("Log out"):
        logout()
        st.rerun()


def _home():
    st.title("📬 Outreach Control Panel")
    st.markdown(
        """
Welcome. This is a **control surface**, not a second sending system —
every Preview, Send, and Check Replies action here either runs the exact
same `outreach.py` logic directly (Preview) or triggers the same GitHub
Actions workflows you'd run manually (Send, Check Replies). Nothing here
bypasses the safety checks already built into the repo: duplicate
protection, per-account capacity, header-verified reply matching, and the
typed `SEND` confirmation gate.

Use the sidebar to navigate:

- **📈 Overview** — every campaign at a glance: sent, pending, replies.
- **📊 Dashboard** — deep-dive into one campaign's leads, sends, replies,
  and errors.
- **🚀 Controls** — Preview a batch instantly, trigger a real Send / Check
  Replies run and watch its status, or run maintenance tools.
- **📧 Email Accounts** — which sender accounts are configured and how
  much each has sent today (never actual credentials).
- **➕ New Campaign** — create a campaign's templates, or add the next
  stage to an existing one. Live immediately — no GitHub approval step.
        """
    )


# Explicit titles/icons here (not embedded in filenames) — filename-embedded
# emoji is what caused the sidebar icons to render as broken/garbled
# characters for some people (a filesystem/encoding issue, not a Streamlit
# bug). Page files themselves now have plain ASCII names.
home_page = st.Page(_home, title="Home", icon="📬", default=True)
overview_page = st.Page("pages/overview.py", title="Overview", icon="📈")
dashboard_page = st.Page("pages/dashboard.py", title="Dashboard", icon="📊")
controls_page = st.Page("pages/controls.py", title="Controls", icon="🚀")
email_accounts_page = st.Page("pages/email_accounts.py", title="Email Accounts", icon="📧")
new_campaign_page = st.Page("pages/new_campaign.py", title="New Campaign", icon="➕")

nav = st.navigation([home_page, overview_page, dashboard_page, controls_page,
                     email_accounts_page, new_campaign_page])
nav.run()
